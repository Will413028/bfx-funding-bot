from dataclasses import replace
from decimal import Decimal

import httpx
import pytest


def evidence(*, rate="1", now=1000, symbol="fUST"):
    from bfx_funding_bot.external.bitfinex.funding_rules import RULE, FundingAmountEvidence
    return FundingAmountEvidence(RULE.digest, symbol, Decimal(rate), now, now)


class FixedRules:
    """Explicit offline FX observations; no environment/venue fallback."""
    def __init__(self, clock=lambda: 1000, rate="1"):
        self.clock, self.rate = clock, rate

    async def observe(self, symbol):
        return evidence(rate=self.rate, now=self.clock(), symbol=symbol)


@pytest.mark.parametrize("rate,minimum", [("1", "150"), ("0.999865", "150.02025274"),
                                        ("0.5", "300"), ("2", "75")])
def test_official_usd_minimum_uses_observed_fx_without_parity(rate, minimum):
    from bfx_funding_bot.external.bitfinex.funding_rules import minimum_amount, validate_amount
    proof = evidence(rate=rate)
    assert minimum_amount(proof, symbol="fUST", now_ms=1000) == Decimal(minimum)
    validate_amount(Decimal(minimum), proof, symbol="fUST", now_ms=1000)
    with pytest.raises(ValueError):
        validate_amount(Decimal(minimum) - Decimal("0.00000001"), proof, symbol="fUST", now_ms=1000)


@pytest.mark.parametrize("fault", ["missing", "version", "zero", "negative", "nan", "infinity",
                                   "symbol", "stale", "future", "reversed"])
def test_invalid_fx_or_changed_rule_blocks(fault):
    from bfx_funding_bot.external.bitfinex.funding_rules import minimum_amount
    proof = evidence()
    fields = {
        "version": {"rule_digest": "old-rule"}, "zero": {"usd_per_unit": Decimal(0)},
        "negative": {"usd_per_unit": Decimal(-1)}, "nan": {"usd_per_unit": Decimal("NaN")},
        "infinity": {"usd_per_unit": Decimal("Infinity")}, "symbol": {"symbol": "fUSD"},
        "stale": {"requested_at_ms": -29001}, "future": {"received_at_ms": 1001},
        "reversed": {"requested_at_ms": 1001},
    }
    proof = None if fault == "missing" else replace(proof, **fields[fault])
    with pytest.raises(ValueError):
        minimum_amount(proof, symbol="fUST", now_ms=1000)


def test_precision_underflow_never_rounds_up_to_minimum():
    from bfx_funding_bot.external.bitfinex.funding_rules import validate_amount
    from bfx_funding_bot.modules.execution.deployment.sizing import venue_amount
    amount = venue_amount(Decimal("149.999999999"))
    assert amount == Decimal("149.99999999")
    with pytest.raises(ValueError):
        validate_amount(amount, evidence(), symbol="fUST", now_ms=1000)
    with pytest.raises(ValueError):
        validate_amount(Decimal("150.000000001"), evidence(), symbol="fUST", now_ms=1000)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ['[]', '[0]', '[-1]', '[true]', '["1"]', '[NaN]', '[Infinity]', '{}', '[1,2]'])
async def test_public_fx_invalid_response_is_not_authority(body):
    from bfx_funding_bot.external.bitfinex.funding_rules import FundingRules
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=body))) as http:
        with pytest.raises(ValueError):
            await FundingRules(http=http, clock=lambda: 1000).observe("fUST")


@pytest.mark.asyncio
@pytest.mark.parametrize("delay,allowed", [(30000, True), (30001, False)])
async def test_fx_is_unauthenticated_and_ages_from_request_start(delay, allowed):
    import json

    from bfx_funding_bot.external.bitfinex.funding_rules import FundingRules, minimum_amount
    now = 1000
    def handler(request):
        nonlocal now
        assert str(request.url) == "https://api-pub.bitfinex.com/v2/calc/fx"
        assert request.method == "POST"
        assert json.loads(request.content) == {"ccy1": "UST", "ccy2": "USD"}
        assert not any(key in request.headers for key in ("bfx-apikey", "bfx-signature", "authorization"))
        now += delay
        return httpx.Response(200, text="[0.999865]")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
        auth=("synthetic", "synthetic"), headers={"bfx-apikey": "synthetic", "authorization": "synthetic"}) as http:
        rules = FundingRules(http=http, clock=lambda: now)
        if allowed:
            proof = await rules.observe("fUST")
            assert proof.requested_at_ms == 1000
            assert minimum_amount(proof, symbol="fUST", now_ms=now) == Decimal("150.02025274")
        else:
            with pytest.raises(ValueError):
                await rules.observe("fUST")
