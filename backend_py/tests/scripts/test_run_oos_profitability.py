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
    # CANARY_YAML resolves relative to cwd (backend_py/); load via absolute path
    yaml_path = Path(__file__).resolve().parents[2] / "configs" / "cells.canary.yaml"
    cells = mod.load_cells_only(yaml_path)
    # canary runs fUST (live) + fUSD (DRY-RUN 2026-06-02: cap>0 but USD unfunded ->
    # per-symbol balance gate hard-blocks fUSD). Both currencies use the same
    # MeanReversion a30 + p2 set (p30-sparse + RatePercentile excluded).
    assert len(cells) == 4
    assert {c.period_agg for c in cells} == {"a30", "p2"}
    assert {c.symbol for c in cells} == {"fUST", "fUSD"}
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
