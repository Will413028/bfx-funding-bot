from decimal import Decimal

from bfx_funding_bot.modules.live_validation.live_attribution import (
    ClampDiagnostic,
    VerdictState,
    check_deployment_anchor,
    check_nav_anchor,
    decide_verdict,
)


def _diag(peak: str) -> ClampDiagnostic:
    return ClampDiagnostic(
        cap=Decimal("570"),
        peak_concurrent=Decimal(peak),
        raw_interest=Decimal("0.5"),
        clamped_interest=Decimal("0.33") if Decimal(peak) > Decimal("570") else Decimal("0.5"),
    )


def _verdict(state_kwargs):  # type: ignore[no-untyped-def]
    base: dict = {
        "headline_active_spread": Decimal("0.06"),
        "n_windows": 10,
        "total_capital_days": Decimal("4000"),
        "ci_lo": Decimal("0.01"),
        "ci_hi": Decimal("0.10"),
        "deployment_anchor": check_deployment_anchor(
            attributed_deployed=Decimal("300"),
            observed_realized=Decimal("300"),
            tol=Decimal("0.05"),
        ),
        "nav_anchor": check_nav_anchor(
            nav_delta=None, attributed_interest=Decimal("0"), tol=Decimal("0.1")
        ),
        "min_windows": 8,
        "min_capital_days": Decimal("3990"),
    }
    base.update(state_kwargs)
    return decide_verdict(**base)


def test_render_markdown_contains_verdict_and_headline():
    from scripts.run_g3_live_validation import render_markdown

    v = _verdict({})
    md = render_markdown(
        verdict=v, data_window="2026-05-30..2026-06-30", n_fills=12, clamp_diag=_diag("400")
    )
    assert "PASS" in md
    assert "0.06" in md
    assert "G3 Live Validation" in md


def test_render_markdown_insufficient_data_states_caveat():
    from scripts.run_g3_live_validation import render_markdown

    v = _verdict({"n_windows": 3})
    assert v.state is VerdictState.INSUFFICIENT_DATA
    md = render_markdown(verdict=v, data_window="n/a", n_fills=0, clamp_diag=_diag("400"))
    assert "INSUFFICIENT_DATA" in md


def test_render_markdown_prints_over_deploy_line_when_clamped():
    from scripts.run_g3_live_validation import render_markdown

    v = _verdict({"n_windows": 1})
    md = render_markdown(
        verdict=v, data_window="2026-05-24..2026-05-31", n_fills=4, clamp_diag=_diag("863")
    )
    assert "clamped to budget" in md
    assert "863" in md


def test_render_markdown_no_over_deploy_line_when_within_budget():
    from scripts.run_g3_live_validation import render_markdown

    v = _verdict({"n_windows": 1})
    md = render_markdown(verdict=v, data_window="x", n_fills=1, clamp_diag=_diag("400"))
    assert "clamped to budget" not in md
