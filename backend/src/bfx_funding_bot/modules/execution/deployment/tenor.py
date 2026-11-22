"""Long-tenor selection: when an offer goes out at the long tenor instead of the signal's.

Plan `docs/plans/2026-11-22-seven-day-tenor-measurement.md`, step 2. Off by default: the
policy exists only when every `BFX_LONG_TENOR_*` variable is set. With a policy, each
offer the deployment loop is about to price is checked in order; the first condition that
fails keeps the signal's tenor and the existing path runs unchanged:

1. the offer's symbol is the policy's and its envelope allows the long tenor;
2. now is on or after the start date (plus the venue clock tolerance) and before the end date;
3. the long offers filled so far plus those sent this tick stay under the total limit
   (a resting offer is already in `offers_filled`: an offer counts unless it is known
   to have ended unfilled, so it is not added a second time);
4. no long offer is resting and none was sent this tick (one at a time);
5. lent plus resting plus sent this tick plus this offer stays within the amount
   limit, and the ended credits are under the sample limit;
6. the long and the signal tenor both price on the same book, and the long price is at
   least max(minimum daily rate, signal-tenor price).

An unreadable exposure (`None`) never places a long offer.

Step 3 withdraws a managed long offer that is still resting once it has rested for the
policy's timeout, or once the envelope no longer allows the long tenor
(`long_offers_to_withdraw`).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from bfx_funding_bot.core.venue_time import VENUE_CLOCK_TOLERANCE_MS
from bfx_funding_bot.modules.execution.contracts import BlockedExecution
from bfx_funding_bot.modules.execution.deployment.period_pricing import (
    PeriodPricer,
    PriceDecision,
)
from bfx_funding_bot.modules.ledger import (
    LongTenorExposure,
    LongTenorQuery,
    ManagedOffer,
    ManagedOffers,
)
from bfx_funding_bot.modules.market import MarketSnapshot
from bfx_funding_bot.modules.strategy import DecisionPayload

_PREFIX = "BFX_LONG_TENOR_"
_KEYS = ("SYMBOL", "DAYS", "MIN_DAILY_RATE", "MAX_AMOUNT", "MAX_OFFERS_TOTAL",
         "MAX_ENDED_CREDITS", "SINCE", "UNTIL", "OFFER_TIMEOUT_S")
_MAX_VENUE_PERIOD_DAYS = 120
_STRATEGY_PERIOD_DAYS = 2


@dataclass(frozen=True, slots=True)
class LongTenorPolicy:
    symbol: str
    period_days: int
    min_daily_rate: Decimal
    max_amount: Decimal
    max_offers_total: int
    max_ended_credits: int
    since_ms: int  # UTC midnight of BFX_LONG_TENOR_SINCE
    until_ms: int  # UTC midnight of BFX_LONG_TENOR_UNTIL: no long offer from then on
    offer_timeout_s: int

    def query(self) -> LongTenorQuery:
        return LongTenorQuery(
            symbol=self.symbol, period_days=self.period_days, since_ms=self.since_ms,
        )


def _midnight_ms(raw: str, name: str) -> int:
    try:
        day = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be YYYY-MM-DD, got {raw!r}") from exc
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


def _positive_int(raw: str, name: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _positive_decimal(raw: str, name: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be positive, got {raw!r}")
    return value


def long_tenor_policy_from_env(environ: Mapping[str, str]) -> LongTenorPolicy | None:
    """None when none is set; some but not all, or an invalid value, fails the boot.

    A half-configured experiment must not boot silently off.
    """
    raw = {key: environ.get(_PREFIX + key, "").strip() for key in _KEYS}
    if not any(raw.values()):
        return None
    missing = [_PREFIX + key for key, value in raw.items() if not value]
    if missing:
        raise ValueError(f"long tenor is partly configured; missing {', '.join(missing)}")
    days = _positive_int(raw["DAYS"], _PREFIX + "DAYS")
    if not _STRATEGY_PERIOD_DAYS < days <= _MAX_VENUE_PERIOD_DAYS:
        raise ValueError(f"{_PREFIX}DAYS must be in 3..{_MAX_VENUE_PERIOD_DAYS}, got {days}")
    since_ms = _midnight_ms(raw["SINCE"], _PREFIX + "SINCE")
    until_ms = _midnight_ms(raw["UNTIL"], _PREFIX + "UNTIL")
    if until_ms <= since_ms:
        raise ValueError(f"{_PREFIX}UNTIL must be after {_PREFIX}SINCE")
    return LongTenorPolicy(
        symbol=raw["SYMBOL"],
        period_days=days,
        min_daily_rate=_positive_decimal(raw["MIN_DAILY_RATE"], _PREFIX + "MIN_DAILY_RATE"),
        max_amount=_positive_decimal(raw["MAX_AMOUNT"], _PREFIX + "MAX_AMOUNT"),
        max_offers_total=_positive_int(raw["MAX_OFFERS_TOTAL"], _PREFIX + "MAX_OFFERS_TOTAL"),
        max_ended_credits=_positive_int(
            raw["MAX_ENDED_CREDITS"], _PREFIX + "MAX_ENDED_CREDITS"),
        since_ms=since_ms,
        until_ms=until_ms,
        offer_timeout_s=_positive_int(raw["OFFER_TIMEOUT_S"], _PREFIX + "OFFER_TIMEOUT_S"),
    )


@dataclass(frozen=True, slots=True)
class LongTenorLoad:
    """A count and an amount of long offers: resting at the venue, or sent this tick."""

    count: int = 0
    amount: Decimal = Decimal(0)

    def plus(self, amount: Decimal) -> LongTenorLoad:
        return LongTenorLoad(self.count + 1, self.amount + amount)


def resting_long_offers(found: ManagedOffers, query: LongTenorQuery) -> LongTenorLoad:
    """Our live offers that may be of the long tenor, from the ledger's managed offers.

    Fail closed: an offer whose period the venue did not report counts, and so does
    an offer whose provenance contradicts itself (its amount is unknown, so only its
    count). A rate the venue did not report does not matter here.
    """
    count, amount = len(found.provenance_conflicts), Decimal(0)
    for offer in found.offers:
        if (offer.symbol == query.symbol
                and offer.period_days in (None, query.period_days)
                and offer.mts_created >= query.since_ms - VENUE_CLOCK_TOLERANCE_MS):
            count += 1
            amount += offer.amount_remaining
    return LongTenorLoad(count, amount)


def withdrawable_long_offers(
    found: ManagedOffers, query: LongTenorQuery,
) -> tuple[ManagedOffer, ...]:
    """The managed offers the venue reports at exactly the long tenor: what step 3 may cancel.

    Narrower than `resting_long_offers` on purpose: an offer of unknown period may be a
    signal-tenor offer, and a conflicting one has no provenance a cancel could name. Not
    limited to `since_ms`: a long offer from before the window is withdrawn all the same.
    """
    return tuple(offer for offer in found.offers
                 if offer.symbol == query.symbol and offer.period_days == query.period_days)


@dataclass(frozen=True, slots=True)
class LongTenorState:
    """What the deployment reads about the long tenor, in one transaction."""

    exposure: LongTenorExposure | None  # None: unreadable; no long offer goes out
    resting: LongTenorLoad
    withdrawable: tuple[ManagedOffer, ...] = ()


def long_offers_to_withdraw(
    policy: LongTenorPolicy,
    offers: tuple[ManagedOffer, ...],
    *,
    now_ms: int,
    envelope_max_period_days: int | None,
) -> tuple[tuple[ManagedOffer, str], ...]:
    """Each long offer to cancel and why ("envelope" or "timeout"), oldest first.

    `envelope_max_period_days` None means the envelope could not be read: only the
    timeout applies then. The tenor is checked here again, so a caller passing other
    offers cannot cancel a signal-tenor offer.
    """
    envelope_refuses = (envelope_max_period_days is not None
                        and envelope_max_period_days < policy.period_days)
    # The venue stamps mts_created; wait out the clock tolerance before calling it timed out.
    timeout_ms = policy.offer_timeout_s * 1000 + VENUE_CLOCK_TOLERANCE_MS
    out: list[tuple[ManagedOffer, str]] = []
    for offer in sorted(offers, key=lambda o: o.mts_created):
        if offer.symbol != policy.symbol or offer.period_days != policy.period_days:
            continue
        if envelope_refuses:
            out.append((offer, "envelope"))
        elif now_ms - offer.mts_created >= timeout_ms:
            out.append((offer, "timeout"))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class TenorChoice:
    """`decision` and `price` are set only when the long tenor was chosen."""

    evidence: Mapping[str, object]
    decision: DecisionPayload | None = None
    price: PriceDecision | None = None


def _refused(condition: int, why: str, **extra: object) -> TenorChoice:
    return TenorChoice(evidence={"selected_days": None, "failed_condition": condition,
                                 "why": why, **extra})


@dataclass(frozen=True, slots=True)
class TenorInputs:
    """What one offer's tenor choice reads, besides the policy and the candidate."""

    envelope_max_period_days: int
    state: LongTenorState | None  # None: not configured or unreadable
    this_tick: LongTenorLoad
    now_ms: int
    snapshot: MarketSnapshot | None


def _schedule_refusal(policy: LongTenorPolicy, inputs: TenorInputs) -> TenorChoice | None:
    """Conditions 1-2: the envelope allows the tenor and the end date has not come."""
    if inputs.envelope_max_period_days < policy.period_days:
        return _refused(1, "envelope",
                        envelope_max_period_days=inputs.envelope_max_period_days)
    if inputs.now_ms >= policy.until_ms:
        return _refused(2, "until")
    # Every count starts at since_ms: an offer placed before it would never be counted.
    if inputs.now_ms < policy.since_ms + VENUE_CLOCK_TOLERANCE_MS:
        return _refused(2, "since")
    return None


def _limits_refusal(
    policy: LongTenorPolicy, candidate: DecisionPayload, inputs: TenorInputs,
) -> TenorChoice | None:
    """Conditions 3-5: the total, one-at-a-time, amount and sample limits."""
    if inputs.state is None or inputs.state.exposure is None:
        return _refused(3, "exposure_unreadable")
    exposure, resting, this_tick = inputs.state.exposure, inputs.state.resting, inputs.this_tick
    if exposure.offers_filled + this_tick.count >= policy.max_offers_total:
        return _refused(3, "offers_total", offers_filled=exposure.offers_filled)
    if resting.count + this_tick.count >= 1:
        return _refused(4, "one_at_a_time", resting=resting.count)
    amount = candidate.offer_amount_usdt or Decimal(0)
    committed = exposure.lent_amount + resting.amount + this_tick.amount + amount
    if committed > policy.max_amount:
        return _refused(5, "amount", committed=str(committed))
    if exposure.ended_credits >= policy.max_ended_credits:
        return _refused(5, "ended_credits", ended_credits=exposure.ended_credits)
    return None


def _rate_of(price: PriceDecision | BlockedExecution) -> str | None:
    return str(price.rate) if isinstance(price, PriceDecision) else None


def choose_tenor(
    policy: LongTenorPolicy | None,
    candidate: DecisionPayload,
    inputs: TenorInputs,
    pricer: PeriodPricer,
) -> TenorChoice | None:
    """None when no policy covers the candidate's symbol: nothing to record."""
    if policy is None or candidate.symbol != policy.symbol:
        return None
    refusal = _schedule_refusal(policy, inputs) or _limits_refusal(policy, candidate, inputs)
    if refusal is not None:
        return refusal
    if inputs.snapshot is None:
        return _refused(6, "no_book")
    long_candidate = candidate.model_copy(update={"offer_duration_days": policy.period_days})
    long_price = pricer.price(candidate=long_candidate, snapshot=inputs.snapshot)
    base_price = pricer.price(candidate=candidate, snapshot=inputs.snapshot)
    prices = {"long_rate": _rate_of(long_price), "base_rate": _rate_of(base_price)}
    if not isinstance(long_price, PriceDecision) or not isinstance(base_price, PriceDecision):
        return _refused(6, "unpriced", **prices)
    if long_price.rate < max(policy.min_daily_rate, base_price.rate):
        return _refused(6, "rate", **prices)
    return TenorChoice(
        evidence={"selected_days": policy.period_days, "failed_condition": None, **prices},
        decision=long_candidate,
        price=long_price,
    )

__all__ = [
    "LongTenorLoad",
    "LongTenorPolicy",
    "LongTenorState",
    "TenorChoice",
    "TenorInputs",
    "choose_tenor",
    "long_offers_to_withdraw",
    "long_tenor_policy_from_env",
    "resting_long_offers",
    "withdrawable_long_offers",
]
