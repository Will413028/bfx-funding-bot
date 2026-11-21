import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_oos_profitability.py"
_SPEC = importlib.util.spec_from_file_location("run_oos_profitability", _SCRIPT_PATH)
mod = importlib.util.module_from_spec(_SPEC)  # type: ignore[arg-type]
assert _SPEC and _SPEC.loader
sys.modules["run_oos_profitability"] = mod  # needed so @dataclass can resolve __module__
_SPEC.loader.exec_module(mod)  # type: ignore[union-attr]


def test_canary_yaml_loads_cells():
    # CANARY_YAML resolves relative to cwd (backend/); load via absolute path
    yaml_path = Path(__file__).resolve().parents[2] / "configs" / "cells.live.yaml"
    cells = mod.load_cells_only(yaml_path)
    # Recovery canary runs fUST only; fUSD stays dark until a separate reviewed
    # change provides explicit coverage. fUST uses the MeanReversion a30 + p2
    # set (p30-sparse + RatePercentile excluded).
    assert len(cells) == 2
    assert {c.period_agg for c in cells} == {"a30", "p2"}
    assert {c.symbol for c in cells} == {"fUST"}
    assert all(c.strategy.value == "mean_reversion" for c in cells)


def test_render_markdown_handles_infinity_sortino_and_ir():
    # The normal lending case: no down months -> Sortino and IR are +Infinity.
    inf = Decimal("Infinity")
    report = mod.CellReport(
        cell_label="fUST_p2",
        n_windows=3,
        strat_summary=mod.OosSummary(
            n_windows=3, median_monthly=Decimal("0.5"), p25_monthly=Decimal("0.3"),
            worst_monthly=Decimal("0.1"), best_monthly=Decimal("0.9"),
            mean_monthly=Decimal("0.5"), annualized_pct=Decimal("6.2"),
            idle_rate=Decimal("0.1"), mean_fill_rate=Decimal("0.8"), sortino=inf,
        ),
        base_summary=mod.OosSummary(
            n_windows=3, median_monthly=Decimal("0.4"), p25_monthly=Decimal("0.2"),
            worst_monthly=Decimal("0.05"), best_monthly=Decimal("0.8"),
            mean_monthly=Decimal("0.4"), annualized_pct=Decimal("4.9"),
            idle_rate=Decimal("0"), mean_fill_rate=Decimal("0.9"), sortino=inf,
        ),
        active=mod.ActiveReturnSummary(
            n_windows=3, median_active=Decimal("0.1"), mean_active=Decimal("0.1"),
            information_ratio=inf, pct_months_outperform=Decimal("0.66"),
        ),
        median_ci=(Decimal("0.3"), Decimal("0.7")),
        deflated_sharpe=Decimal("0.97"),
        n_trials=9,
    )
    md = mod.render_markdown([report], data_window="2022-01..2026-05")
    assert "Infinity" in md


def test_render_markdown_contains_key_sections():
    report = mod.CellReport(
        cell_label="fUST_a30",
        n_windows=3,
        strat_summary=mod.OosSummary(
            n_windows=3, median_monthly=Decimal("0.5"), p25_monthly=Decimal("0.3"),
            worst_monthly=Decimal("0.1"), best_monthly=Decimal("0.9"),
            mean_monthly=Decimal("0.5"), annualized_pct=Decimal("6.2"),
            idle_rate=Decimal("0.1"), mean_fill_rate=Decimal("0.8"), sortino=Decimal("1.2"),
        ),
        base_summary=mod.OosSummary(
            n_windows=3, median_monthly=Decimal("0.4"), p25_monthly=Decimal("0.2"),
            worst_monthly=Decimal("0.05"), best_monthly=Decimal("0.8"),
            mean_monthly=Decimal("0.4"), annualized_pct=Decimal("4.9"),
            idle_rate=Decimal("0"), mean_fill_rate=Decimal("0.9"), sortino=Decimal("1.0"),
        ),
        active=mod.ActiveReturnSummary(
            n_windows=3, median_active=Decimal("0.1"), mean_active=Decimal("0.1"),
            information_ratio=Decimal("0.5"), pct_months_outperform=Decimal("0.66"),
        ),
        median_ci=(Decimal("0.3"), Decimal("0.7")),
        deflated_sharpe=Decimal("0.97"),
        n_trials=9,
    )
    md = mod.render_markdown([report], data_window="2022-01..2026-05")
    assert "OOS honesty caveat" in md
    assert "Non-backtestable risk register" in md
    assert "fUST_a30" in md
    assert "Selection bias" in md
    # bot-vs-idle reframe: headline is the primary metric, MR alpha is secondary
    assert "bot-vs-idle" in md
    assert "MR timing alpha" in md
    assert "secondary diagnostic" in md
    # the demoted active section no longer leads with the old heading
    assert "### Active return vs passive" not in md
    # idle arm is 0% by construction note is present
    assert "idle arm" in md.lower()


def _a_report(label: str):
    summary = mod.OosSummary(
        n_windows=3, median_monthly=Decimal("0.5"), p25_monthly=Decimal("0.3"),
        worst_monthly=Decimal("0.1"), best_monthly=Decimal("0.9"),
        mean_monthly=Decimal("0.5"), annualized_pct=Decimal("6.2"),
        idle_rate=Decimal("0.1"), mean_fill_rate=Decimal("0.8"), sortino=Decimal("1.5"),
    )
    return mod.CellReport(
        cell_label=label, n_windows=3, strat_summary=summary, base_summary=summary,
        active=mod.ActiveReturnSummary(
            n_windows=3, median_active=Decimal("0.1"), mean_active=Decimal("0.1"),
            information_ratio=Decimal("0.5"), pct_months_outperform=Decimal("0.66"),
        ),
        median_ci=(Decimal("0.3"), Decimal("0.7")), deflated_sharpe=Decimal("0.97"), n_trials=42,
    )


def _run_main_without_a_database(monkeypatch, tmp_path, extra_args):
    """Run `_amain` with the DB replaced: each cell returns a fixed report and its candles."""
    import asyncio
    import json
    from contextlib import asynccontextmanager

    from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
    from bfx_funding_bot.modules.backtest.run_record import fingerprint_candles

    class _Engine:
        async def dispose(self) -> None:
            return None

    @asynccontextmanager
    async def _scope(_factory):
        yield None

    async def _run_cell(_session, cell, _n_trials):
        candles = [FundingCandle(symbol=cell.symbol, timeframe="1h", period_agg=cell.period_agg,
                                 mts=mts, close="0.0001") for mts in (1, 2)]
        return _a_report(cell.cell_id), fingerprint_candles(f"candles:{cell.cell_id}", candles)

    monkeypatch.setattr(mod, "Settings", lambda: None)
    monkeypatch.setattr(mod, "make_engine", lambda _settings: _Engine())
    monkeypatch.setattr(mod, "make_session_factory", lambda _engine: None)
    monkeypatch.setattr(mod, "session_scope", _scope)
    monkeypatch.setattr(mod, "_run_cell", _run_cell)
    out = tmp_path / "oos.md"
    cells = Path(__file__).resolve().parents[2] / "configs" / "cells.live.yaml"
    monkeypatch.setattr(sys, "argv", ["run_oos_profitability", "--output", str(out),
                                      "--cells", str(cells), *extra_args])
    assert asyncio.run(mod._amain()) == 0
    return json.loads(out.with_suffix(".run.json").read_text())


def test_the_weekly_invocation_leaves_a_run_record(monkeypatch, tmp_path):
    record = _run_main_without_a_database(monkeypatch, tmp_path, [])
    assert record["registry_entry"] == mod.DEFAULT_REGISTRY_ENTRY
    assert record["runner"] == "run_oos_profitability"
    assert record["params"]["start_mts"] == mod.START_MTS
    assert len(record["params"]["cells_sha256"]) == 64
    assert "n_trials" not in record["params"]
    assert len(record["inputs"]) == 2
    assert all(item["kind"] == "candles" and item["items"] == 2 for item in record["inputs"])
    assert [o["name"] for o in record["outputs"]] == ["oos.md", "oos.json"]


def test_an_ad_hoc_run_names_its_registry_entry_and_is_another_config(monkeypatch, tmp_path):
    weekly = _run_main_without_a_database(monkeypatch, tmp_path, [])
    ad_hoc = _run_main_without_a_database(monkeypatch, tmp_path, ["--registry-entry", "MeanReversion"])
    more_trials = _run_main_without_a_database(monkeypatch, tmp_path, ["--n-trials", "99"])
    assert ad_hoc["registry_entry"] == "MeanReversion"
    assert ad_hoc["config_sha256"] != weekly["config_sha256"]
    assert more_trials["config_sha256"] == weekly["config_sha256"]


def test_a_cells_file_without_cells_fails_before_writing_anything(monkeypatch, tmp_path):
    import asyncio

    class _Engine:
        async def dispose(self) -> None:
            return None

    monkeypatch.setattr(mod, "Settings", lambda: None)
    monkeypatch.setattr(mod, "make_engine", lambda _settings: _Engine())
    monkeypatch.setattr(mod, "make_session_factory", lambda _engine: None)
    monkeypatch.setattr(mod, "load_cells_only", lambda _path: [])
    out = tmp_path / "oos.md"
    monkeypatch.setattr(sys, "argv", ["run_oos_profitability", "--output", str(out)])
    assert asyncio.run(mod._amain()) == 2
    assert list(tmp_path.iterdir()) == []
