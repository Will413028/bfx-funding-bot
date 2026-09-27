from dataclasses import replace
from decimal import Decimal

from bfx_funding_bot.modules.live_validation.credit_attribution import WeeklyReconciliation
from bfx_funding_bot.modules.live_validation.live_attribution import (
    METHODOLOGY_CHANGE_DATE,
    CreditCoverage,
    DeploymentCheck,
    FrrBenchmark,
    G3Report,
    VerdictState,
    decide_verdict,
)
from scripts.run_g3_live_validation import _verdict_to_json, render_markdown

_FEE = Decimal("0.15")
_MON = 1789948800000   # 2026-09-21


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


def _week(*, flagged: bool = False, complete: bool = True) -> WeeklyReconciliation:
    return WeeklyReconciliation(
        currency="UST", week_start_ms=_MON, credit_gross=Decimal("0.0447"),
        credit_net=Decimal("0.038"), ledger_net=Decimal("0.10") if flagged else Decimal("0.038"),
        payouts=2, diff=Decimal("-0.062") if flagged else Decimal("0"), diff_pct=None,
        complete=complete, flagged=flagged,
    )


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
    # also on the header line, so a reader of the TL;DR alone sees it
    assert md.index("not comparable with earlier reports") < md.index("## TL;DR")


def test_render_markdown_names_an_explicit_capital():
    md = render_markdown(report=_report(capital_source="--capital 570"), fee_rate=_FEE)
    assert "fixed, from --capital 570" in md


def test_render_markdown_credit_coverage():
    md = render_markdown(report=_report(), fee_rate=_FEE)
    assert "- fUST_p2: gross interest 0.00029488" in md
    assert "unattributed (no trade / offer not ours, excluded): 1 credits" in md


def test_render_markdown_reconciliation_table_and_flag():
    ok = render_markdown(report=_report(), fee_rate=_FEE)
    assert "complete weeks: 1, flagged: 0" in ok
    assert "| 2026-09-21 | 0.038000 | 0.038000 | +0.000000 | ok |" in ok

    bad = _report({"ledger_divergence": ["week 2026-09-21 credits net 0.038 vs ledger 0.1"]},
                  reconciliations=[_week(flagged=True)])
    md = render_markdown(report=bad, fee_rate=_FEE)
    assert "Verdict: UNRELIABLE" in md
    assert "| FLAG |" in md


def test_render_markdown_reconciliation_unavailable():
    md = render_markdown(report=_report(reconciliations=[], reconciliation_available=False),
                         fee_rate=_FEE)
    assert "no ledger payouts; the credit model is unchecked" in md


def test_render_markdown_mr_alpha_section_available():
    md = render_markdown(report=_report(), fee_rate=_FEE)
    assert "MR timing alpha" in md
    assert "secondary diagnostic" in md
    assert "0% by construction" in md
    assert "MR alpha 95% CI: [-0.01, 0.02]" in md


def test_render_markdown_mr_alpha_section_unavailable():
    md = render_markdown(report=_report({"mr_alpha_available": False}), fee_rate=_FEE)
    assert "MR timing alpha" in md
    assert "unavailable" in md
    assert "MR alpha spread" not in md  # numeric lines must NOT render when unavailable


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
    j = _verdict_to_json(_report(peak="863"), fee_rate=_FEE)
    assert j["methodology"] == {
        "model": "venue_credits_actual_held_time", "since": METHODOLOGY_CHANGE_DATE,
        "comparable_with_reports_before": False, "capital_source": "ledger",
    }
    assert j["deployment"] == {"cap": "570", "peak_open_principal": "863", "over_deployed": True}
    assert j["credit_coverage"]["gross_by_cell"] == {"fUST_p2": "0.00029488"}
    assert j["reconciliation"]["available"] is True
    assert j["reconciliation"]["weeks"][0]["flagged"] is False
    assert "over_deploy" not in j
    assert j["headline_bot_vs_idle"] == "0.06"
    assert j["mr_alpha"] == {"spread": "0.0", "ci_lo": "-0.01", "ci_hi": "0.02",
                             "available": True}
