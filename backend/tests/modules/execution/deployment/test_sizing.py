from decimal import Decimal

from bfx_funding_bot.modules.execution.deployment.sizing import effective_min_usdt

D = Decimal


def test_effective_min_static_153():
    assert effective_min_usdt(D("150"), D("0.02")) == D("153")


def test_effective_min_rounds_up():
    # 150 * 1.015 = 152.25 -> ceil -> 153
    assert effective_min_usdt(D("150"), D("0.015")) == D("153")

