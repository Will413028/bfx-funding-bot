"""Versioned local funding validity, not a guarantee of venue acceptance.

USD150 and eight decimals are documented rules. Applying public UST/USD FX
to funding equivalence is an explicit local inference; the venue does not
document its internal valuation. Evidence ages from request START, locally
bounded at 30s. No peg assumption, environment floor, buffer or retry.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, Decimal
from hashlib import sha256
from typing import Protocol

import httpx


@dataclass(frozen=True, slots=True)
class FundingRule:
    version: str = "bitfinex-funding-usd-fx-v1"
    minimum_usd: Decimal = Decimal("150")
    amount_quantum: Decimal = Decimal("0.00000001")
    fx_max_age_ms: int = 30000
    sources: tuple[str, ...] = (
        "https://support.bitfinex.com/hc/en-us/articles/213918949-What-is-the-minimum-offer-for-Funding",
        "https://docs.bitfinex.com/docs/introduction",
        "https://docs.bitfinex.com/reference/rest-public-foreign-exchange-rate",
    )

    @property
    def digest(self) -> str:
        return sha256(json.dumps(asdict(self), sort_keys=True, default=str).encode()).hexdigest()


RULE = FundingRule()


@dataclass(frozen=True, slots=True)
class FundingAmountEvidence:
    rule_digest: str
    symbol: str
    usd_per_unit: Decimal
    requested_at_ms: int
    received_at_ms: int

    def payload(self) -> dict[str, object]:
        return {**asdict(self), "usd_per_unit": str(self.usd_per_unit),
                "valuation": "local_public_fx_inference"}


def minimum_amount(evidence: FundingAmountEvidence | None, *, symbol: str, now_ms: int) -> Decimal:
    if not isinstance(evidence, FundingAmountEvidence):
        raise ValueError("funding_rule_or_fx_unavailable")
    if (evidence.rule_digest != RULE.digest
        or evidence.symbol != symbol or symbol not in {"fUST", "fUSD"}
        or not isinstance(evidence.usd_per_unit, Decimal)
        or (symbol == "fUSD" and evidence.usd_per_unit != 1)
        or not evidence.usd_per_unit.is_finite() or evidence.usd_per_unit <= 0
        or type(evidence.requested_at_ms) is not int or type(evidence.received_at_ms) is not int
        or not evidence.requested_at_ms <= evidence.received_at_ms <= now_ms
        or now_ms - evidence.requested_at_ms > RULE.fx_max_age_ms):
        raise ValueError("funding_rule_or_fx_unavailable")
    return (RULE.minimum_usd / evidence.usd_per_unit).quantize(RULE.amount_quantum, rounding=ROUND_CEILING)


def validate_amount(amount: Decimal, evidence: FundingAmountEvidence | None, *, symbol: str, now_ms: int) -> None:
    minimum = minimum_amount(evidence, symbol=symbol, now_ms=now_ms)
    if (not isinstance(amount, Decimal) or not amount.is_finite() or amount < minimum
        or amount != amount.quantize(RULE.amount_quantum)):
        raise ValueError("funding_amount_below_minimum_or_invalid_precision")


class FundingRuleProvider(Protocol):
    async def observe(self, symbol: str) -> FundingAmountEvidence: ...


class FundingRules:
    def __init__(self, *, http: httpx.AsyncClient, clock: Callable[[], int]) -> None:
        self._http, self._clock = http, clock

    async def observe(self, symbol: str) -> FundingAmountEvidence:
        if symbol == "fUSD":
            now = self._clock()
            return FundingAmountEvidence(RULE.digest, symbol, Decimal("1"), now, now)
        if symbol != "fUST":
            raise ValueError("funding_currency_rule_unavailable")
        started = self._clock()
        # A standalone request inherits neither default auth headers nor cookies
        # from the daemon's shared client; send explicitly disables client auth.
        request = httpx.Request("POST", "https://api-pub.bitfinex.com/v2/calc/fx",
                                json={"ccy1": "UST", "ccy2": "USD"})
        response = await self._http.send(request, auth=None, follow_redirects=False)
        response.raise_for_status()
        body = json.loads(response.text, parse_float=Decimal, parse_int=Decimal)
        if not isinstance(body, list) or len(body) != 1 or not isinstance(body[0], Decimal):
            raise ValueError("funding_fx_response_invalid")
        evidence = FundingAmountEvidence(RULE.digest, symbol, body[0], started, self._clock())
        minimum_amount(evidence, symbol=symbol, now_ms=self._clock())
        return evidence
