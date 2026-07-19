import importlib.util
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from bfx_funding_bot.modules.backtest.frr_series import FrrSeries
from bfx_funding_bot.modules.backtest.oos_profitability import WindowOutcome
from bfx_funding_bot.modules.backtest.wfo import compute_wfo_windows
from bfx_funding_bot.modules.candles.schemas import FundingCandle

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_frr_floor_backtest.py"
_SPEC = importlib.util.spec_from_file_location("run_frr_floor_backtest", _SCRIPT_PATH)
mod = importlib.util.module_from_spec(_SPEC)  # type: ignore[arg-type]
assert _SPEC and _SPEC.loader
sys.modules["run_frr_floor_backtest"] = mod  # needed so @dataclass can resolve __module__
_SPEC.loader.exec_module(mod)  # type: ignore[union-attr]

H = 3_600_000


def _candles(n_hours: int, close: str = "0.0004") -> list[FundingCandle]:
    start = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)
    return [
        FundingCandle(
            symbol="fUST", timeframe="1h", period_agg="p2", mts=start + i * H,
            open=Decimal(close), close=Decimal(close),
            high=Decimal(close), low=Decimal(close), volume=Decimal("100"),
        )
        for i in range(n_hours)
    ]


def _series_matching(close: str, n_hours: int) -> FrrSeries:
    # stored frr = per-day close / 365 -> frr*365 == close (ratio 1.0)
    start = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)
    frr_raw = Decimal(close) / Decimal("365")
    return FrrSeries([(start + i * H, frr_raw) for i in range(0, n_hours, 1)])


def test_evaluate_arms_returns_aligned_outcomes_per_arm() -> None:
    candles = _candles(24 * 30 * 6)  # 6 months
    windows = compute_wfo_windows(candles)
    assert windows
    series = _series_matching("0.0004", 24 * 30 * 6)
    factories = mod.arm_factories(
        ema_span=24, threshold_sigma=Decimal("0.5"), ratio_sigma=Decimal("0.4"),
        frr_at=series.at,
    )
    assert set(factories) == {mod.ARM_MR, mod.ARM_FLOOR, mod.ARM_AMR, mod.ARM_FRR}
    outcomes = mod.evaluate_arms(candles, windows, factories)
    assert set(outcomes) == set(factories)
    lengths = {len(v) for v in outcomes.values()}
    assert lengths == {len(windows)}
    for arm_outcomes in outcomes.values():
        for o, w in zip(arm_outcomes, windows, strict=True):
            assert o.month_mts == w.test_start_mts
    # flat series: MR never skips -> floor == mr; always_frr rate == close -> same as AMR
    for a, b in zip(outcomes[mod.ARM_MR], outcomes[mod.ARM_FLOOR], strict=True):
        assert a.net_monthly == b.net_monthly
    assert all(o.net_monthly > 0 for o in outcomes[mod.ARM_FRR])


def test_audit_frr_unit_passes_on_matching_scale() -> None:
    n = 24 * 40
    candles = _candles(n)
    audit = mod.audit_frr_unit(candles, _series_matching("0.0004", n))
    assert audit.ok
    assert len(audit.per_year) == 1
    year_row = audit.per_year[0]
    assert year_row.year == 2025
    assert abs(year_row.med_ratio - Decimal("1")) < Decimal("0.01")


def test_audit_frr_unit_fails_on_wrong_unit() -> None:
    # stats already per-day (no /365 storage) -> frr*365 is 365x too big
    n = 24 * 40
    candles = _candles(n)
    start = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)
    wrong = FrrSeries([(start + i * H, Decimal("0.0004")) for i in range(n)])
    audit = mod.audit_frr_unit(candles, wrong)
    assert not audit.ok


def _outcomes(vals: list[str]) -> list[WindowOutcome]:
    return [
        WindowOutcome(
            month_mts=int(datetime(2025, i + 1, 1, tzinfo=UTC).timestamp() * 1000),
            net_monthly=Decimal(v), n_trades=3, fill_rate=Decimal("1"),
        )
        for i, v in enumerate(vals)
    ]


def _report() -> object:
    outcomes = {
        mod.ARM_MR: _outcomes(["0.4", "0.5", "0.6"]),
        mod.ARM_FLOOR: _outcomes(["0.5", "0.6", "0.7"]),
        mod.ARM_AMR: _outcomes(["0.45", "0.55", "0.65"]),
        mod.ARM_FRR: _outcomes(["0.3", "0.4", "0.5"]),
    }
    audit = mod.FrrUnitAudit(
        per_year=[mod.YearAudit(year=2025, n=100, med_frr_raw=Decimal("1e-6"),
                                med_close=Decimal("0.0004"), med_ratio=Decimal("0.5"))],
        ok=True,
    )
    return mod.build_symbol_report(
        symbol="fUST", outcomes=outcomes, unit_audit=audit,
        data_window="2025-01 .. 2025-04",
    )


def test_build_symbol_report_computes_pairs_and_per_year() -> None:
    r = _report()
    assert r.n_windows == 3
    floor_vs_mr = r.pair_summaries["mr_frr_floor_vs_mr"]
    assert floor_vs_mr.mean_active == Decimal("0.1")
    assert floor_vs_mr.pct_months_outperform == Decimal("1")
    floor_vs_frr = r.pair_summaries["mr_frr_floor_vs_always_frr"]
    assert floor_vs_frr.mean_active == Decimal("0.2")
    assert r.per_year_mean_monthly[mod.ARM_MR][2025] == Decimal("0.5")


def test_render_markdown_contains_key_sections() -> None:
    md = mod.render_markdown([_report()], data_window="2025-01 .. 2025-04")
    assert "FRR unit audit" in md
    assert "Honesty caveats" in md
    assert "fUST" in md
    for label in (mod.ARM_MR, mod.ARM_FLOOR, mod.ARM_AMR, mod.ARM_FRR):
        assert label in md
    assert "mr_frr_floor_vs_mr" in md
    assert "mr_frr_floor_vs_always_frr" in md
    assert "15%" in md  # fee caveat
    assert "fill" in md.lower()


def test_deployed_mr_params_reads_canary_yaml() -> None:
    yaml_path = Path(__file__).resolve().parents[2] / "configs" / "cells.canary.yaml"
    cells = mod.load_cells_only(yaml_path)
    p = mod.deployed_mr_params(cells, symbol="fUST", period_agg="p2")
    assert p["ema_span"] == 24
    assert p["threshold_sigma"] == Decimal("0.5")
    assert p["ratio_sigma"] == Decimal("0.4062263004196277")
