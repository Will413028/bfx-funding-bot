from decimal import Decimal

from bfx_funding_bot.modules.live_validation.live_attribution import (
    VerdictState,
    check_deployment_anchor,
    check_nav_anchor,
    decide_verdict,
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
    md = render_markdown(verdict=v, data_window="2026-05-30..2026-06-30", n_fills=12)
    assert "PASS" in md
    assert "0.06" in md
    assert "G3 Live Validation" in md


def test_render_markdown_insufficient_data_states_caveat():
    from scripts.run_g3_live_validation import render_markdown

    v = _verdict({"n_windows": 3})
    assert v.state is VerdictState.INSUFFICIENT_DATA
    md = render_markdown(verdict=v, data_window="n/a", n_fills=0)
    assert "INSUFFICIENT_DATA" in md
