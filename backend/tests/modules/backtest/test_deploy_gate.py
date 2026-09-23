from decimal import Decimal

from bfx_funding_bot.modules.backtest.deploy_gate import evaluate_gate


def test_inert_config_fails_distinguishability():
    # active identically 0: IR==0, no month outperforms -> fails (a)
    r = evaluate_gate(
        information_ratio=Decimal("0"), pct_outperform=Decimal("0"),
        mean_active_ci_low=Decimal("0"),
    )
    assert r.passed is False
    assert r.distinguishable is False
    assert r.not_worse is True  # ci_low==0 passes the default >= 0 floor; criteria are orthogonal


def test_worse_than_passive_fails_not_worse():
    # does something (IR<0) but CI low < 0 -> fails (b)
    r = evaluate_gate(
        information_ratio=Decimal("-0.4"), pct_outperform=Decimal("0.2"),
        mean_active_ci_low=Decimal("-0.03"),
    )
    assert r.passed is False
    assert r.distinguishable is True
    assert r.not_worse is False


def test_good_config_passes():
    r = evaluate_gate(
        information_ratio=Decimal("0.5"), pct_outperform=Decimal("0.84"),
        mean_active_ci_low=Decimal("0.01"),
    )
    assert r.passed is True


def test_noisy_but_ok_passes_default_fails_strict():
    # positive point estimate but CI lower bound sits at 0
    common = {"information_ratio": Decimal("0.1"), "pct_outperform": Decimal("0.55")}
    assert evaluate_gate(mean_active_ci_low=Decimal("0"), **common).passed is True
    assert evaluate_gate(mean_active_ci_low=Decimal("0"), strict=True, **common).passed is False
