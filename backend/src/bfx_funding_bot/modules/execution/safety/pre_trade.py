"""Always-on pre-trade limits (lending envelope ADR 2026-09-25 D1).

Independent of the capital budget and of the trading state: these bound what a
single new offer may look like and how fast the writer may talk to the venue,
so a broken signal, sizing bug or retry loop cannot turn into a large, cheap or
long-dated loan, or a flood of venue writes.

``offer_envelope`` checks one new offer against the applied CapitalPolicy's
``max_offer_amount`` and ``envelope`` (DB, versioned, amended by the operator):
amount ceiling, period bounds, open managed offers per symbol and the rate floor
max(min_rate_apr / 365, median live bid x rate_floor_ratio). A policy without an
envelope, an unreadable policy or projection, no fresh book, or a malformed
decision refuses. ``CommandThrottle`` bounds submits and cancels; its limits are
platform configuration (``pre_trade_limits.command_rate``) because they protect
the venue API, not the lending terms.

The envelope never applies to a cancel (see ``chain._CANCEL_EXEMPT``): pulling
an offer must always be possible, and a cancel's probe carries the managed
offer's old terms.
"""
from __future__ import annotations

import logging
import statistics
import time
from collections import deque
from collections.abc import Callable
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.errors import ConfigurationError
from bfx_funding_bot.modules.execution.capital_policy import CapitalPolicy
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
from bfx_funding_bot.modules.observability import alerts
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload

log = logging.getLogger(__name__)

OFFER_ENVELOPE = "offer_envelope"
PRE_TRADE_GUARD_NAMES = frozenset({OFFER_ENVELOPE})


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


class OfferEnvelopeGuard:
    """One new offer against the applied policy's envelope; anything unknown refuses.

    The market's bids are the independent rate reference (Bitfinex publishes no
    FRR on this feed); the absolute floor also catches a market-wide collapse or
    a rate unit bug that would drag the relative floor down with it.
    """
    name = OFFER_ENVELOPE

    def __init__(self, *, runtime: CapitalRuntime, book: FundingBookProvider,
                 clock: Callable[[], int]) -> None:
        self._runtime = runtime
        self._book = book
        self._clock = clock

    async def _policy_and_open(self, session: AsyncSession,
                               symbol: str) -> tuple[CapitalPolicy, int]:
        repository = self._runtime.repository
        policy = await read_policy_unlocked(
            session, account_id=repository.account_id,
            environment=repository.environment, symbol=symbol)
        count = await session.scalar(select(func.count()).select_from(VenueOfferStateRow).where(
            VenueOfferStateRow.exchange_account_id == repository.account_id,
            VenueOfferStateRow.deployment_environment == repository.environment,
            VenueOfferStateRow.symbol == symbol,
            VenueOfferStateRow.is_terminal.is_(False),
            # Managed offers only (D2): a manual offer never takes a slot.
            VenueOfferStateRow.execution_decision_id.is_not(None),
        ))
        return policy, int(count or 0)

    def _block(self, reason: str) -> GuardResult:
        return GuardResult(False, self.name, reason)

    async def evaluate(self, decision: DecisionPayload, ctx: AccountContext) -> GuardResult:
        if decision.decision_outcome != DecisionOutcome.POST:
            return GuardResult(True, self.name)
        amount = decision.offer_amount_usdt
        if amount is None or not amount.is_finite() or amount <= 0:
            return self._block("offer_amount_invalid")
        rate = decision.offer_rate
        if rate is None or not rate.is_finite() or rate <= 0:
            return self._block("offer_rate_invalid")
        try:
            if ctx.command_session is not None:
                policy, open_offers = await self._policy_and_open(
                    ctx.command_session, decision.symbol)
            else:
                async with self._runtime.session_factory() as session:
                    policy, open_offers = await self._policy_and_open(session, decision.symbol)
        except Exception as exc:
            return self._block(f"policy_unavailable: {exc}")
        envelope = policy.envelope
        if envelope is None or policy.max_offer_amount is None:
            return self._block("envelope_unset")
        if amount > policy.max_offer_amount:
            return self._block(f"offer_amount {amount} > max_offer_amount "
                               f"{policy.max_offer_amount}")
        period = decision.offer_duration_days
        if (type(period) is not int
                or not envelope.min_period_days <= period <= envelope.max_period_days):
            return self._block(f"period {period} outside {envelope.min_period_days}.."
                               f"{envelope.max_period_days} days")
        if open_offers >= envelope.max_open_offers:
            return self._block(f"open_offers {open_offers} >= limit {envelope.max_open_offers}")
        try:
            snapshot = self._book.snapshot(decision.symbol, now_ms=self._clock())
        except Exception as exc:
            return self._block(f"rate_reference_unavailable: {exc}")
        if (snapshot is None or snapshot.symbol != decision.symbol
                or not snapshot.sequence_valid or not snapshot.checksum_valid):
            return self._block("rate_reference_unavailable: no fresh consistent book")
        reference = reference_bid_rate(snapshot.bids, period_days=period)
        if reference is None:
            return self._block("rate_reference_unavailable: no bids")
        floor = max(envelope.min_daily_rate, reference * envelope.rate_floor_ratio)
        if rate < floor:
            return self._block(f"offer_rate {rate} < floor {floor} (median bid {reference} x "
                               f"{envelope.rate_floor_ratio}, absolute "
                               f"{envelope.min_daily_rate})")
        return GuardResult(True, self.name)


class CommandThrottle:
    """Token bucket over venue writes (submit + cancel) at the command gate.

    ``admit`` is synchronous and cheap; it runs while the account command lock
    is held. A refused submit is alerted; ``trip_blocks`` refused submits within
    ``trip_window_s`` mean the writer keeps pushing past the limit, which is a
    runaway, not a burst -- that trips HALTED/auto once per episode. A refused
    cancel waits for the next token and never counts toward that stop.
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
        if kind == "cancel":
            # Pulling exposure never counts toward the stop: a HALTED caused by
            # throttling would itself need cancels to converge (D3).
            log.warning("command_throttled kind=cancel (not counted toward a halt)")
            return False
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
    """A live writer never starts without its command throttle."""
    if cfg is None:
        raise PreTradeConfigurationError(
            "safety config has no pre_trade_limits; a live writer refuses to start without them")
    return cfg


def build_pre_trade_guards(*, runtime: CapitalRuntime, book: FundingBookProvider | None,
                           clock: Callable[[], int]) -> list[OfferEnvelopeGuard]:
    """The envelope guard; a live writer needs a book for the rate floor."""
    if book is None:
        raise PreTradeConfigurationError("offer_envelope needs the live funding book")
    return [OfferEnvelopeGuard(runtime=runtime, book=book, clock=clock)]


def build_command_throttle(cfg: PreTradeLimitsCfg, *, protection: ProtectionPort | None,
                           clock: Callable[[], float] = time.monotonic) -> CommandThrottle:
    rate = cfg.command_rate
    return CommandThrottle(capacity=rate.capacity, refill_per_second=rate.refill_per_second,
                           trip_blocks=rate.trip_blocks, trip_window_s=rate.trip_window_seconds,
                           protection=protection, clock=clock)


__all__ = [
    "OFFER_ENVELOPE",
    "PRE_TRADE_GUARD_NAMES",
    "CommandThrottle",
    "OfferEnvelopeGuard",
    "PreTradeConfigurationError",
    "build_command_throttle",
    "build_pre_trade_guards",
    "reference_bid_rate",
    "require_pre_trade_limits",
]
