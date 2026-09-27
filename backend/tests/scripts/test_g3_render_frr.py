from decimal import Decimal

from bfx_funding_bot.modules.live_validation.live_attribution import (
    CreditCoverage,
    DeploymentCheck,
    FrrBenchmark,
    G3Report,
    G3Verdict,
    VerdictState,
)
from scripts.run_g3_live_validation import _verdict_to_json, render_markdown


def _verdict() -> G3Verdict:
    return G3Verdict(
        state=VerdictState.INSUFFICIENT_DATA,
        headline_bot_vs_idle=Decimal("0.04"), n_windows=5,
        ci_lo=Decimal("0"), ci_hi=Decimal("0"), reasons=["only 5 weekly windows"],
        mr_alpha_spread=Decimal("0.005"), mr_alpha_ci_lo=Decimal("0"),
        mr_alpha_ci_hi=Decimal("0"), mr_alpha_available=True,
    )


def _report(frr: FrrBenchmark) -> G3Report:
    return G3Report(
        verdict=_verdict(), data_window="w", capital_source="ledger",
        coverage=CreditCoverage(4, {}, 0, Decimal("0"), 0),
        deployment=DeploymentCheck(cap=Decimal("10000"), peak_open_principal=Decimal("500")),
        frr=frr, reconciliations=[], reconciliation_available=False,
    )


def test_render_includes_frr_benchmark_section():
    frr = FrrBenchmark(
        available=True, spread=Decimal("0.01"),
        ci_lo=Decimal("-0.005"), ci_hi=Decimal("0.02"), reason=None,
    )
    md = render_markdown(report=_report(frr), fee_rate=Decimal("0.15"))
    assert "AlwaysFRR benchmark" in md
    assert "value bar" in md
    assert "歷史政策" in md
    assert "0.01" in md


def test_render_frr_unavailable_shows_reason():
    frr = FrrBenchmark(
        available=False, spread=Decimal("0"), ci_lo=Decimal("0"),
        ci_hi=Decimal("0"), reason="funding_stats empty — run backfill (E3 Task 8)",
    )
    md = render_markdown(report=_report(frr), fee_rate=Decimal("0.15"))
    assert "unavailable" in md.lower()
    assert "funding_stats empty" in md


def test_render_includes_fee_adjusted_headline():
    frr = FrrBenchmark(
        available=False, spread=Decimal("0"), ci_lo=Decimal("0"),
        ci_hi=Decimal("0"), reason="x",
    )
    md = render_markdown(report=_report(frr), fee_rate=Decimal("0.15"))
    # 0.04 × 0.85 = 0.034
    assert "fee-adjusted" in md
    assert "0.034" in md


def test_json_includes_frr_and_fee_keys():
    frr = FrrBenchmark(
        available=True, spread=Decimal("0.01"),
        ci_lo=Decimal("-0.005"), ci_hi=Decimal("0.02"), reason=None,
    )
    j = _verdict_to_json(_report(frr), fee_rate=Decimal("0.15"))
    assert j["frr_benchmark"]["available"] is True
    assert j["frr_benchmark"]["spread"] == "0.01"
    assert j["fee_adjusted"]["fee_rate"] == "0.15"
    assert j["fee_adjusted"]["headline_bot_vs_idle_net"] == str(
        Decimal("0.04") * Decimal("0.85")
    )
