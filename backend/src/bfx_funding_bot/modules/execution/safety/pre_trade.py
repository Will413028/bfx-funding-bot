"""Always-on pre-trade limits (T9, plan §1, ADR 2026-09-25 D5).

Independent of the capital budget and of the trading state: these bound what a
single new offer may look like and how fast the writer may talk to the venue,
so a broken signal, sizing bug or retry loop cannot turn into a large, cheap or
long-dated loan, or a flood of venue writes.

========================== =================================================
guard / control            refuses
========================== =================================================
``period_bounds``          a period outside the configured [min, max] days
``max_offer_amount``       an offer above the applied CapitalPolicy's absolute
                           ceiling -- and every offer while none is set
``open_offer_limit``       a new offer once the symbol has that many open offers
``rate_floor``             a rate below ``ratio x`` the median live bid (exact
                           period, else the whole book) -- and every offer when
                           no fresh book is available to compare with
``CommandThrottle``        submits and cancels beyond a token bucket; repeated
                           refusals inside a window trip HALTED/auto
========================== =================================================

Every guard is fail-closed: a missing symbol configuration, an unreadable
policy or projection, or a malformed decision refuses. None of them applies to
a cancel (see ``chain._CANCEL_EXEMPT``): pulling an offer must always be
possible, and a cancel's probe carries the managed offer's old terms.
"""
from __future__ import annotations

import logging
import statistics
import time
from collections import deque
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.errors import ConfigurationError
from bfx_funding_bot.modules.execution.capital_repository import read_policy_unlocked
from bfx_funding_bot.modules.execution.capital_runtime import CapitalRuntime
from bfx_funding_bot.modules.execution.event_store.tables import VenueOfferStateRow
from bfx_funding_bot.modules.execution.protocols import AccountContext, GuardResult
from bfx_funding_bot.modules.execution.safety.config import PreTradeLimitsCfg
from bfx_funding_bot.modules.execution.safety.protection import (
    COMMAND_RATE_EXCEEDED,
    ProtectionPort,
)
from bfx_funding_bot.modules.marketfeed.funding_book import FundingBookProvider
from bfx_funding_bot.modules.marketfeed.schemas import DecisionOutcome, DecisionPayload
from bfx_funding_bot.modules.observability import alerts

log = logging.getLogger(__name__)

PERIOD_BOUNDS = "period_bounds"
MAX_OFFER_AMOUNT = "max_offer_amount"
OPEN_OFFER_LIMIT = "open_offer_limit"
RATE_FLOOR = "rate_floor"
PRE_TRADE_GUARD_NAMES = frozenset({PERIOD_BOUNDS, MAX_OFFER_AMOUNT, OPEN_OFFER_LIMIT, RATE_FLOOR})

# Bitfinex accepts funding offers of 2..120 days; config cannot widen this.
VENUE_MIN_PERIOD_DAYS = 2
VENUE_MAX_PERIOD_DAYS = 120


class PreTradeConfigurationError(ConfigurationError):
    """A live writer was about to start without its always-on limits."""


def _decimal(value: float | None) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _allow(name: str) -> GuardResult:
    return GuardResult(True, name)


def _block(name: str, reason: str) -> GuardResult:
    return GuardResult(False, name, reason)


class PeriodBoundsGuard:
    name = PERIOD_BOUNDS
    is_calibrated = False

    def __init__(self, *, bounds: Mapping[str, tuple[int, int]]) -> None:
        self._bounds = dict(bounds)

    async def evaluate(self, decision: DecisionPayload, ctx: AccountContext) -> GuardResult:
        if decision.decision_outcome != DecisionOutcome.POST:
            return _allow(self.name)
        bounds = self._bounds.get(decision.symbol)
        if bounds is None:
            return _block(self.name, f"period_limits_unconfigured: {decision.symbol}")
        low, high = max(bounds[0], VENUE_MIN_PERIOD_DAYS), min(bounds[1], VENUE_MAX_PERIOD_DAYS)
        period = decision.offer_duration_days
        if type(period) is not int or not low <= period <= high:
            return _block(self.name, f"period {period} outside {low}..{high} days")
        return _allow(self.name)


class MaxOfferAmountGuard:
    """The applied CapitalPolicy's absolute per-offer ceiling; none set refuses."""
    name = MAX_OFFER_AMOUNT
    is_calibrated = False

    def __init__(self, *, runtime: CapitalRuntime) -> None:
        self._runtime = runtime

    async def evaluate(self, decision: DecisionPayload, ctx: AccountContext) -> GuardResult:
        if decision.decision_outcome != DecisionOutcome.POST:
            return _allow(self.name)
        amount = _decimal(decision.offer_amount_usdt)
        if amount is None or amount <= 0:
            return _block(self.name, "offer_amount_invalid")
        repository = self._runtime.repository
        try:
            if ctx.command_session is not None:
                policy = await read_policy_unlocked(
                    ctx.command_session, account_id=repository.account_id,
                    environment=repository.environment, symbol=decision.symbol)
            else:
                async with self._runtime.session_factory() as session:
                    policy = await read_policy_unlocked(
                        session, account_id=repository.account_id,
                        environment=repository.environment, symbol=decision.symbol)
        except Exception as exc:
            return _block(self.name, f"policy_unavailable: {exc}")
        if policy.max_offer_amount is None:
            return _block(self.name, "max_offer_amount_unset")
        if amount > policy.max_offer_amount:
            return _block(self.name, f"offer_amount {amount} > max_offer_amount "
                                     f"{policy.max_offer_amount}")
        return _allow(self.name)


class OpenOfferLimitGuard:
    """No new offer once the symbol already has ``max_open_offers`` open at the venue."""
    name = OPEN_OFFER_LIMIT
    is_calibrated = False

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession], account_id: UUID,
                 environment: str, limits: Mapping[str, int]) -> None:
        self._sf = session_factory
        self._account_id = account_id
        self._environment = environment
        self._limits = dict(limits)

    async def _open(self, session: AsyncSession, symbol: str) -> int:
        count = await session.scalar(select(func.count()).select_from(VenueOfferStateRow).where(
            VenueOfferStateRow.exchange_account_id == self._account_id,
            VenueOfferStateRow.deployment_environment == self._environment,
            VenueOfferStateRow.symbol == symbol,
            VenueOfferStateRow.is_terminal.is_(False),
        ))
        return int(count or 0)

    async def evaluate(self, decision: DecisionPayload, ctx: AccountContext) -> GuardResult:
        if decision.decision_outcome != DecisionOutcome.POST:
            return _allow(self.name)
        limit = self._limits.get(decision.symbol)
        if limit is None:
            return _block(self.name, f"open_offer_limit_unconfigured: {decision.symbol}")
        try:
            if ctx.command_session is not None:
                open_offers = await self._open(ctx.command_session, decision.symbol)
            else:
                async with self._sf() as session:
                    open_offers = await self._open(session, decision.symbol)
        except Exception as exc:
            return _block(self.name, f"open_offers_unreadable: {exc}")
        if open_offers >= limit:
            return _block(self.name, f"open_offers {open_offers} >= limit {limit}")
        return _allow(self.name)


def reference_bid_rate(bids: tuple[object, ...], *, period_days: int) -> Decimal | None:
    """Median bid rate at the offer's exact period, else across the whole book."""
    def rates(levels: list[object]) -> list[Decimal]:
        values = []
        for level in levels:
            rate = _decimal(getattr(level, "rate", None))
            if rate is not None and rate > 0:
                values.append(rate)
        return values

    exact = rates([level for level in bids if getattr(level, "period", None) == period_days])
    pool = exact or rates(list(bids))
    return statistics.median(pool) if pool else None


class RateFloorGuard:
    """Refuse a rate below ``ratio x`` the median live bid; no fresh book refuses.

    Closes the Phase 0 gap: E2's lower bound is relative to the signal, and the
    TAKER branch prices at the signal whenever a bid pays at least that much,
    so an abnormally low signal would lend at that low rate. The market's bids
    are the independent reference -- Bitfinex publishes no FRR on this feed.
    """
    name = RATE_FLOOR
    is_calibrated = False

    def __init__(self, *, book: FundingBookProvider, ratios: Mapping[str, Decimal],
                 clock: Callable[[], int]) -> None:
        self._book = book
        self._ratios = dict(ratios)
        self._clock = clock

    async def evaluate(self, decision: DecisionPayload, ctx: AccountContext) -> GuardResult:
        if decision.decision_outcome != DecisionOutcome.POST:
            return _allow(self.name)
        ratio = self._ratios.get(decision.symbol)
        if ratio is None:
            return _block(self.name, f"rate_floor_unconfigured: {decision.symbol}")
        rate = _decimal(decision.offer_rate)
        if rate is None or rate <= 0:
            return _block(self.name, "offer_rate_invalid")
        try:
            snapshot = self._book.snapshot(decision.symbol, now_ms=self._clock())
        except Exception as exc:
            return _block(self.name, f"rate_reference_unavailable: {exc}")
        if (snapshot is None or snapshot.symbol != decision.symbol
                or not snapshot.sequence_valid or not snapshot.checksum_valid):
            return _block(self.name, "rate_reference_unavailable: no fresh consistent book")
        reference = reference_bid_rate(snapshot.bids,
                                       period_days=decision.offer_duration_days or 0)
        if reference is None:
            return _block(self.name, "rate_reference_unavailable: no bids")
        floor = reference * ratio
        if rate < floor:
            return _block(self.name, f"offer_rate {rate} < floor {floor} "
                                     f"(median bid {reference} x {ratio})")
        return _allow(self.name)


class CommandThrottle:
    """Token bucket over venue writes (submit + cancel) at the command gate.

    ``admit`` is synchronous and cheap; it runs while the account command lock
    is held. A refused command is alerted; ``trip_blocks`` refusals within
    ``trip_window_s`` mean the writer keeps pushing past the limit, which is a
    runaway, not a burst -- that trips HALTED/auto once per episode.
    """

    def __init__(self, *, capacity: int, refill_per_second: float, trip_blocks: int,
                 trip_window_s: float, protection: ProtectionPort | None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if capacity <= 0 or refill_per_second <= 0 or trip_blocks <= 0 or trip_window_s <= 0:
            raise ValueError("command throttle parameters must be positive")
        self._capacity = float(capacity)
        self._refill = refill_per_second
        self._trip_blocks = trip_blocks
        self._window = trip_window_s
        self._protection = protection
        self._clock = clock
        self._tokens = float(capacity)
        self._refilled_at = clock()
        self._blocks: deque[float] = deque()
        self._tripped = False

    def bind(self, protection: ProtectionPort) -> None:
        self._protection = protection

    def admit(self, kind: str) -> bool:
        now = self._clock()
        self._tokens = min(self._capacity,
                           self._tokens + max(0.0, now - self._refilled_at) * self._refill)
        self._refilled_at = now
        while self._blocks and now - self._blocks[0] > self._window:
            self._blocks.popleft()
        if not self._blocks:
            self._tripped = False
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        self._blocks.append(now)
        blocked = len(self._blocks)
        log.warning("command_throttled kind=%s blocked_in_window=%d", kind, blocked)
        alerts.emit("command_throttled", level=alerts.WARNING, kind=kind,
                    blocked_in_window=blocked, window_seconds=int(self._window))
        if blocked >= self._trip_blocks and not self._tripped and self._protection is not None:
            self._tripped = True
            self._protection.trip(COMMAND_RATE_EXCEEDED,
                                  f"{blocked} submit/cancel commands throttled within "
                                  f"{int(self._window)}s (last: {kind})")
        return False


def require_pre_trade_limits(cfg: PreTradeLimitsCfg | None) -> PreTradeLimitsCfg:
    """A live writer never starts without its always-on limits."""
    if cfg is None:
        raise PreTradeConfigurationError(
            "safety config has no pre_trade_limits; a live writer refuses to start without them")
    return cfg


def build_pre_trade_guards(
    cfg: PreTradeLimitsCfg, *, runtime: CapitalRuntime, book: FundingBookProvider | None,
    session_factory: async_sessionmaker[AsyncSession], account_id: UUID, environment: str,
    clock: Callable[[], int],
) -> list[PeriodBoundsGuard | MaxOfferAmountGuard | OpenOfferLimitGuard | RateFloorGuard]:
    """The four guards in cheap-first order; a live writer needs a book for the floor."""
    if book is None:
        raise PreTradeConfigurationError("rate_floor needs the live funding book")
    symbols = cfg.symbols
    return [
        PeriodBoundsGuard(bounds={s: (c.min_period_days, c.max_period_days)
                                  for s, c in symbols.items()}),
        MaxOfferAmountGuard(runtime=runtime),
        OpenOfferLimitGuard(session_factory=session_factory, account_id=account_id,
                            environment=environment,
                            limits={s: c.max_open_offers for s, c in symbols.items()}),
        RateFloorGuard(book=book, ratios={s: c.rate_floor_ratio for s, c in symbols.items()},
                       clock=clock),
    ]


def build_command_throttle(cfg: PreTradeLimitsCfg, *, protection: ProtectionPort | None,
                           clock: Callable[[], float] = time.monotonic) -> CommandThrottle:
    rate = cfg.command_rate
    return CommandThrottle(capacity=rate.capacity, refill_per_second=rate.refill_per_second,
                           trip_blocks=rate.trip_blocks, trip_window_s=rate.trip_window_seconds,
                           protection=protection, clock=clock)


__all__ = [
    "MAX_OFFER_AMOUNT",
    "OPEN_OFFER_LIMIT",
    "PERIOD_BOUNDS",
    "PRE_TRADE_GUARD_NAMES",
    "RATE_FLOOR",
    "CommandThrottle",
    "MaxOfferAmountGuard",
    "OpenOfferLimitGuard",
    "PeriodBoundsGuard",
    "PreTradeConfigurationError",
    "RateFloorGuard",
    "build_command_throttle",
    "build_pre_trade_guards",
    "reference_bid_rate",
    "require_pre_trade_limits",
]
