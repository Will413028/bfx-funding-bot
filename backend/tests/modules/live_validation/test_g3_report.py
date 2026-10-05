from dataclasses import replace
from decimal import Decimal

from bfx_funding_bot.modules.live_validation.credit_attribution import WeeklyReconciliation
from bfx_funding_bot.modules.live_validation.g3_report import render_markdown, verdict_to_json
from bfx_funding_bot.modules.live_validation.live_attribution import (
    METHODOLOGY_CHANGE_DATE,
    CellMrAlpha,
    CreditCoverage,
    DataThreshold,
    DeploymentCheck,
    FrrBenchmark,
    G3Report,
    VerdictState,
    decide_verdict,
)

_FEE = Decimal("0.15")
_MON = 1789948800000   # 2026-09-21
_WEEK = 7 * 86_400_000
_GATE = [_MON - i * _WEEK for i in reversed(range(8))]


def _frr() -> FrrBenchmark:
    return FrrBenchmark(
        available=True, spread=Decimal("0.01"),
        ci_lo=Decimal("-0.005"), ci_hi=Decimal("0.02"), reason=None,
    )


def _verdict(state_kwargs):  # type: ignore[no-untyped-def]
    base: dict = {
        "headline_bot_vs_idle": Decimal("0.06"),
        "n_windows": 10,
        "total_capital_days": Decimal("4000"),
        "ci_lo": Decimal("0.01"),
        "ci_hi": Decimal("0.10"),
        "ledger_divergence": [],
        "min_windows": 8,
        "min_capital_days": Decimal("3990"),
        "mr_alpha_spread": Decimal("0.0"),
        "mr_alpha_ci_lo": Decimal("-0.01"),
        "mr_alpha_ci_hi": Decimal("0.02"),
        "mr_alpha_available": True,
    }
    base.update(state_kwargs)
    return decide_verdict(**base)


def _week(*, week: int = _MON, flagged: bool = False, complete: bool = True) -> WeeklyReconciliation:
    return WeeklyReconciliation(
        currency="UST", week_start_ms=week, credit_gross=Decimal("0.0447"),
        credit_net=Decimal("0.038"), ledger_net=Decimal("0.10") if flagged else Decimal("0.038"),
        payouts=2, diff=Decimal("-0.062") if flagged else Decimal("0"), diff_pct=None,
        complete=complete, flagged=flagged,
    )


_P2 = CellMrAlpha("fUST_p2", "p2", Decimal("0.75"), True, None, Decimal("0.004"),
                  Decimal("-0.001"), Decimal("0.009"))
_A30 = CellMrAlpha("fUST_a30", "a30", Decimal("0.25"), False,
                   "no market-rate coverage in window (fUST/1h/a30 candles) — bot-vs-idle is"
                   " unaffected", Decimal("0"), Decimal("0"), Decimal("0"))


def _report(state_kwargs=None, *, peak: str = "400", **over):  # type: ignore[no-untyped-def]
    base = G3Report(
        verdict=_verdict(state_kwargs or {}),
        data_window="2026-09-22..2026-09-27",
        capital_source="ledger",
        coverage=CreditCoverage(
            bot_credits=12, gross_by_cell={"fUST_p2": Decimal("0.00029488")},
            unattributed_credits=1, unattributed_gross=Decimal("0.0447"), ambiguous_credits=0,
        ),
        deployment=DeploymentCheck(cap=Decimal("570"), peak_open_principal=Decimal(peak)),
        frr=_frr(),
        reconciliations=[_week()],
        reconciliation_available=True,
        mr_alpha_cells=[_P2],
        mr_alpha_coverage=Decimal("1"),
        data_threshold=DataThreshold(
            Decimal("4000"), Decimal("2768.50"),
            "mean ledger wallet balance over the 10 evaluated windows (395.50) × 7 days"),
        gate_weeks=_GATE,
        acknowledgements={},
    )
    return replace(base, **over)


def test_render_markdown_contains_verdict_and_headline():
    md = render_markdown(report=_report(), fee_rate=_FEE)
    assert "PASS" in md
    assert "0.06" in md
    assert "bot-vs-idle" in md
    assert "G3 Live Validation" in md
    assert "bot credits: 12" in md


def test_render_markdown_states_the_methodology_discontinuity():
    md = render_markdown(report=_report(), fee_rate=_FEE)
    assert f"## Methodology change ({METHODOLOGY_CHANGE_DATE})" in md
    assert f"G3 reports dated before {METHODOLOGY_CHANGE_DATE} are not comparable" in md
    assert "MTS_OPENING..MTS_LAST_PAYOUT" in md
    assert "repaid after 14 min" in md
    assert "funding-wallet balance per weekly window" in md
    assert "Minimum data: was C × 7 capital-days" in md
    assert "one per bot cell, matching its period_agg" in md
    # also on the header line, so a reader of the TL;DR alone sees it
    assert md.index("not comparable with earlier reports") < md.index("## TL;DR")


def test_render_markdown_names_an_explicit_capital():
    md = render_markdown(report=_report(capital_source="--capital 570"), fee_rate=_FEE)
    assert "fixed, from --capital 570" in md


def test_render_markdown_credit_coverage():
    md = render_markdown(report=_report(), fee_rate=_FEE)
    assert "- fUST_p2: gross interest 0.00029488" in md
    assert "unattributed (no trade / offer not ours, excluded): 1 credits" in md


def test_render_markdown_states_the_data_threshold():
    md = render_markdown(report=_report(), fee_rate=_FEE)
    assert ("- bot capital-days: 4000.00; minimum 2768.50 = mean ledger wallet balance over"
            " the 10 evaluated windows (395.50) × 7 days.") in md


def test_render_markdown_reconciliation_gate_statuses():
    ok = render_markdown(report=_report(), fee_rate=_FEE)
    assert "gate: the most recent 8 settled weeks (2026-08-03..2026-09-21); blocking flags: 0" in ok
    assert "| 2026-09-21 | 0.038000 | 0.038000 | +0.000000 | ok |" in ok

    old = _MON - 9 * _WEEK
    bad = _report({"ledger_divergence": ["week 2026-09-21 credits net 0.038 vs ledger 0.1"]},
                  reconciliations=[_week(week=old, flagged=True), _week(flagged=True)])
    md = render_markdown(report=bad, fee_rate=_FEE)
    assert "Verdict: UNRELIABLE" in md
    assert "blocking flags: 1" in md
    assert "| 2026-07-20 | 0.038000 | 0.100000 | -0.062000 | FLAG, outside gate |" in md
    assert "| 2026-09-21 | 0.038000 | 0.100000 | -0.062000 | FLAG |" in md


def test_render_markdown_echoes_operator_acknowledgements():
    acks = {_MON: "venue paid a late correction", _MON - _WEEK: "stale ack"}
    md = render_markdown(report=_report(reconciliations=[_week(flagged=True)],
                                        acknowledgements=acks), fee_rate=_FEE)
    assert "| FLAG, acknowledged: venue paid a late correction |" in md
    assert "- 2026-09-21: venue paid a late correction — clears a gate FLAG" in md
    assert "- 2026-09-14: stale ack — unused (no gate FLAG that week)" in md
    j = verdict_to_json(_report(acknowledgements=acks), fee_rate=_FEE)
    assert j["reconciliation"]["acknowledged"] == {  # type: ignore[index]
        "2026-09-14": "stale ack", "2026-09-21": "venue paid a late correction"}


def test_render_markdown_reconciliation_unavailable():
    md = render_markdown(report=_report(reconciliations=[], reconciliation_available=False),
                         fee_rate=_FEE)
    assert "no ledger payouts; the credit model is unchecked" in md


def test_render_markdown_mr_alpha_per_cell_and_total():
    md = render_markdown(report=_report(mr_alpha_cells=[_P2, _A30],
                                        mr_alpha_coverage=Decimal("0.75")), fee_rate=_FEE)
    assert "| fUST_p2 | p2 | 75.00% | 0.004% | [-0.001, 0.009] | |" in md
    assert "| fUST_a30 | a30 | 25.00% | — | — | unavailable: no market-rate coverage" in md
    assert "| **total** | | 75.00% | 0.0% | [-0.01, 0.02] | covers 75.00% of bot capital-days |" in md
    assert "0% by construction" in md


def test_render_markdown_mr_alpha_section_unavailable():
    md = render_markdown(report=_report({"mr_alpha_available": False}, mr_alpha_cells=[_A30],
                                        mr_alpha_coverage=Decimal("0")), fee_rate=_FEE)
    assert "| **total** | | — | — | — | unavailable: no cell has an in-band baseline" in md


def test_render_markdown_insufficient_data_states_caveat():
    report = _report({"n_windows": 3})
    assert report.verdict.state is VerdictState.INSUFFICIENT_DATA
    assert "INSUFFICIENT_DATA" in render_markdown(report=report, fee_rate=_FEE)


def test_render_markdown_over_deploy_caveat_only_above_c():
    over = render_markdown(report=_report(peak="863"), fee_rate=_FEE)
    assert "peaked at 863" in over
    assert "no clamp applied" in over
    assert "OVER C" in over
    within = render_markdown(report=_report(), fee_rate=_FEE)
    assert "peaked at" not in within
    assert "within C" in within
    assert "Held-to-term" not in within


def test_verdict_to_json_carries_methodology_coverage_and_reconciliation():
    j = verdict_to_json(_report(peak="863"), fee_rate=_FEE)
    assert j["methodology"] == {
        "model": "venue_credits_actual_held_time", "since": METHODOLOGY_CHANGE_DATE,
        "comparable_with_reports_before": False, "capital_source": "ledger",
    }
    assert j["deployment"] == {"cap": "570", "peak_open_principal": "863", "over_deployed": True}
    assert j["credit_coverage"]["gross_by_cell"] == {"fUST_p2": "0.00029488"}  # type: ignore[index]
    rec = j["reconciliation"]
    assert rec["available"] is True  # type: ignore[index]
    assert rec["gate_weeks"][-1] == "2026-09-21"  # type: ignore[index]
    assert rec["weeks"][0]["status"] == "ok"  # type: ignore[index]
    assert j["data_threshold"]["minimum"] == "2768.50"  # type: ignore[index]
    assert j["mr_alpha"]["cells"][0]["cell"] == "fUST_p2"  # type: ignore[index]
    assert j["mr_alpha"]["coverage"] == "1"  # type: ignore[index]
    assert j["headline_bot_vs_idle"] == "0.06"


def test_offer_cell_conflicts_are_in_the_markdown_and_the_json():
    conflict = "5123273052: legacy=fUST_p2, journal=fUST_a30"
    report = _report(offer_conflicts=(conflict,))
    assert "1 offer cell conflict(s)" in render_markdown(report=report, fee_rate=_FEE)
    assert conflict in render_markdown(report=report, fee_rate=_FEE)
    assert verdict_to_json(report, fee_rate=_FEE)["offer_cell_conflicts"] == [conflict]
    assert verdict_to_json(_report(), fee_rate=_FEE)["offer_cell_conflicts"] == []
