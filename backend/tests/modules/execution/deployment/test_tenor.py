"""Long-tenor selection rule (plan 2026-11-22 seven-day tenor, step 2)."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.external.bitfinex.funding_book_level import FundingBookLevel
from bfx_funding_bot.modules.execution.deployment.period_pricing import PeriodPricer
from bfx_funding_bot.modules.execution.deployment.tenor import (
    LongTenorLoad,
    LongTenorPolicy,
    LongTenorState,
    TenorChoice,
    TenorInputs,
    choose_tenor,
    long_tenor_policy_from_env,
    resting_long_offers,
)
from bfx_funding_bot.modules.ledger import LongTenorExposure, ManagedOffer, ManagedOffers
from bfx_funding_bot.modules.market import MarketSnapshot
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload

SINCE_MS = 1_763_769_600_000  # 2025-11-22T00:00Z
UNTIL_MS = SINCE_MS + 70 * 86_400_000
NOW_MS = SINCE_MS + 86_400_000
PRICER = PeriodPricer(tick=Decimal("0.00000001"))

ENV = {
    "BFX_LONG_TENOR_SYMBOL": "fUST",
    "BFX_LONG_TENOR_DAYS": "7",
    "BFX_LONG_TENOR_MIN_DAILY_RATE": "0.0002",
    "BFX_LONG_TENOR_MAX_AMOUNT": "600",
    "BFX_LONG_TENOR_MAX_OFFERS_TOTAL": "30",
    "BFX_LONG_TENOR_MAX_ENDED_CREDITS": "20",
    "BFX_LONG_TENOR_SINCE": "2025-11-22",
    "BFX_LONG_TENOR_UNTIL": "2026-01-31",
    "BFX_LONG_TENOR_OFFER_TIMEOUT_S": "1800",
}


def _policy() -> LongTenorPolicy:
    policy = long_tenor_policy_from_env(ENV)
    assert policy is not None
    return replace(policy, until_ms=UNTIL_MS)


def _decision(
    *, amount: str = "200", rate: str = "0.00015", symbol: str = "fUST",
) -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=Decimal(rate),
        offer_amount_usdt=Decimal(amount),
        offer_duration_days=2,
        symbol=symbol,
    )


def _level(rate: float, amount: float, period: int) -> FundingBookLevel:
    return FundingBookLevel(rate=rate, period=period, count=1, amount=amount)


def _book(*levels: FundingBookLevel) -> MarketSnapshot:
    return MarketSnapshot(
        snapshot_id="book-1", symbol="fUST",
        bids=tuple(level for level in levels if level.amount < 0),
        asks=tuple(level for level in levels if level.amount > 0),
        captured_at_ms=NOW_MS, received_at_ms=NOW_MS, source="ws",
        sequence_valid=True, checksum_valid=True, sequence=2,
    )


# 7-day asks at 0.00030 price the long offer at 0.00029999; 2-day asks at 0.00018.
GOOD_BOOK = _book(_level(0.00030, 5_000, 7), _level(0.00018, 5_000, 2))


def _inputs(**overrides: Any) -> TenorInputs:
    """`exposure` / `resting` override the state's parts; `state=None` drops it."""
    exposure = overrides.pop("exposure", LongTenorExposure.none())
    resting = overrides.pop("resting", LongTenorLoad())
    base = TenorInputs(
        envelope_max_period_days=7, state=LongTenorState(exposure, resting),
        this_tick=LongTenorLoad(), now_ms=NOW_MS, snapshot=GOOD_BOOK,
    )
    return replace(base, **overrides)


def _choose(policy: LongTenorPolicy | None = None, decision: DecisionPayload | None = None,
            **overrides: Any) -> TenorChoice | None:
    return choose_tenor(
        policy if policy is not None else _policy(),
        decision if decision is not None else _decision(), _inputs(**overrides), PRICER,
    )


def test_all_conditions_hold_selects_long_tenor_at_its_book_price() -> None:
    choice = _choose()
    assert choice is not None and choice.decision is not None and choice.price is not None
    assert choice.decision.offer_duration_days == 7
    assert choice.price.rate == Decimal("0.00029999")
    assert choice.evidence == {"selected_days": 7, "failed_condition": None,
                               "long_rate": "0.00029999", "base_rate": "0.00017999"}


def test_no_policy_or_other_symbol_records_nothing() -> None:
    assert choose_tenor(None, _decision(), _inputs(), PRICER) is None
    assert _choose(decision=_decision(symbol="fUSD")) is None


@pytest.mark.parametrize(("overrides", "condition", "why"), [
    ({"envelope_max_period_days": 6}, 1, "envelope"),
    ({"now_ms": UNTIL_MS}, 2, "until"),
    ({"state": None}, 3, "exposure_unreadable"),
    ({"exposure": LongTenorExposure(29, Decimal(0), 0),
      "this_tick": LongTenorLoad()}, None, None),
    ({"exposure": LongTenorExposure(30, Decimal(0), 0)}, 3, "offers_total"),
    ({"exposure": LongTenorExposure(28, Decimal(0), 0),
      "this_tick": LongTenorLoad(2, Decimal(0))}, 3, "offers_total"),
    ({"resting": LongTenorLoad(1, Decimal(100))}, 4, "one_at_a_time"),
    ({"this_tick": LongTenorLoad(1, Decimal(100))}, 4, "one_at_a_time"),
    ({"exposure": LongTenorExposure(0, Decimal(400), 0)}, None, None),
    ({"exposure": LongTenorExposure(0, Decimal("400.01"), 0)}, 5, "amount"),
    ({"exposure": LongTenorExposure(0, Decimal(19), 19)}, None, None),
    ({"exposure": LongTenorExposure(0, Decimal(0), 20)}, 5, "ended_credits"),
    ({"snapshot": None}, 6, "no_book"),
])
def test_each_condition_alone_keeps_the_signal_tenor(
    overrides: dict[str, Any], condition: int | None, why: str | None,
) -> None:
    choice = _choose(**overrides)
    assert choice is not None
    assert choice.evidence["failed_condition"] == condition
    assert choice.evidence.get("why") == why
    assert (choice.decision is None) == (condition is not None)


def test_resting_amount_counts_toward_the_amount_limit() -> None:
    # Condition 4 would refuse first with a resting offer, so judge the amount alone.
    policy = _policy()
    choice = _choose(policy, exposure=LongTenorExposure(0, Decimal(300), 0),
                     this_tick=LongTenorLoad(0, Decimal("100.01")))
    assert choice is not None and choice.evidence["why"] == "amount"


def test_long_price_under_the_minimum_rate_keeps_two_days() -> None:
    book = _book(_level(0.00019, 5_000, 7), _level(0.00015, 5_000, 2))
    choice = _choose(snapshot=book)
    assert choice is not None and choice.evidence["why"] == "rate"


def test_long_price_under_the_two_day_price_keeps_two_days() -> None:
    book = _book(_level(0.00025, 5_000, 7), _level(0.00030, 5_000, 2))
    choice = _choose(snapshot=book)
    assert choice is not None and choice.evidence["why"] == "rate"


def test_judged_on_the_priced_rate_not_the_ask() -> None:
    # The 7-day ask is 0.00020001: priced one tick under it, the rate is exactly the
    # minimum; at 0.0002 itself the priced rate drops below and the rule refuses.
    at = _choose(snapshot=_book(_level(0.00020001, 5_000, 7), _level(0.00015, 5_000, 2)))
    under = _choose(snapshot=_book(_level(0.00020, 5_000, 7), _level(0.00015, 5_000, 2)))
    assert at is not None and at.decision is not None
    assert under is not None and under.evidence["why"] == "rate"


def test_taker_bid_at_a_signal_rate_under_the_minimum_does_not_go_long() -> None:
    # A 7-day bid pays the signal rate, so the long offer would take at the signal rate,
    # which is under the minimum.
    book = _book(_level(0.00025, -5_000, 7), _level(0.00030, 5_000, 7),
                 _level(0.00012, 5_000, 2))
    choice = _choose(snapshot=book, decision=_decision(rate="0.00015"))
    assert choice is not None and choice.evidence["why"] == "rate"
    assert choice.evidence["long_rate"] == "0.00015"


def test_two_day_unpriced_keeps_two_days() -> None:
    choice = _choose(snapshot=_book(_level(0.00030, 5_000, 7)))
    assert choice is not None and choice.evidence["why"] == "unpriced"
    assert choice.evidence["base_rate"] is None


def test_long_unpriced_keeps_two_days() -> None:
    choice = _choose(snapshot=_book(_level(0.00018, 5_000, 2)))
    assert choice is not None and choice.evidence["why"] == "unpriced"


def test_policy_from_env_off_when_nothing_set_and_fails_when_partly_set() -> None:
    assert long_tenor_policy_from_env({}) is None
    partial = {k: v for k, v in ENV.items() if k != "BFX_LONG_TENOR_OFFER_TIMEOUT_S"}
    with pytest.raises(ValueError, match="OFFER_TIMEOUT_S"):
        long_tenor_policy_from_env(partial)
    policy = long_tenor_policy_from_env(ENV)
    assert policy is not None
    assert policy.since_ms == SINCE_MS
    assert policy.query().period_days == 7


@pytest.mark.parametrize(("key", "value"), [
    ("BFX_LONG_TENOR_DAYS", "2"),
    ("BFX_LONG_TENOR_DAYS", "121"),
    ("BFX_LONG_TENOR_MIN_DAILY_RATE", "abc"),
    ("BFX_LONG_TENOR_MAX_AMOUNT", "0"),
    ("BFX_LONG_TENOR_MAX_OFFERS_TOTAL", "-1"),
    ("BFX_LONG_TENOR_SINCE", "22/11/2025"),
    ("BFX_LONG_TENOR_UNTIL", "2025-11-22"),
])
def test_policy_from_env_rejects_invalid_values(key: str, value: str) -> None:
    with pytest.raises(ValueError, match=key.removeprefix("BFX_LONG_TENOR_")):
        long_tenor_policy_from_env({**ENV, key: value})


def _managed(*, period: int | None = 7, created: int = NOW_MS, symbol: str = "fUST",
             remaining: str = "150") -> ManagedOffer:
    return ManagedOffer(
        venue_offer_id="1", symbol=symbol, amount_remaining=Decimal(remaining),
        attempt_id=uuid4(), decision_id="d", cell_id="c", signal_correlation_id="s",
        rate=None, rate_observed=False, period_days=period, mts_created=created,
        amount_original=None, status="ACTIVE",
    )


def test_resting_counts_our_offers_of_the_tenor_or_unknown_tenor_since_the_start() -> None:
    found = ManagedOffers(offers=(
        _managed(), _managed(period=None, remaining="10"), _managed(period=2),
        _managed(created=SINCE_MS - 1), _managed(symbol="fUSD"),
    ), provenance_conflicts=())
    assert resting_long_offers(found, _policy().query()) == LongTenorLoad(2, Decimal(160))


def test_resting_counts_a_contradictory_offer_without_its_amount() -> None:
    found = ManagedOffers(offers=(), provenance_conflicts=("x",))
    assert resting_long_offers(found, _policy().query()) == LongTenorLoad(1, Decimal(0))
