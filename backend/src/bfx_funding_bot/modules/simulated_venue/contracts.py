"""Public contracts of the simulated venue: scope, config, fault plan, ports.

This module imports nothing from the bot (ledger, execution, trading, marketfeed):
the venue is an independent counterparty and the ledger is only its test oracle.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol

from bfx_funding_bot.modules.simulated_venue.events import VenueEvent

# The only realms a simulated venue may run in. `prod` is refused here and by the
# `sim_venue_event` CHECK constraint.
ALLOWED_REALMS = ("shadow", "ci")
REQUIRED_AUTHORITY_EPOCH = "ledger"

_SYMBOL = re.compile(r"^f[A-Z0-9]{2,10}$")


class RealmRefusedError(ValueError):
    """The simulated venue refuses to exist in this realm or authority epoch."""


class VenueStoreError(RuntimeError):
    """The store could not append: nothing was recorded (the venue answers 5xx, caller unsure)."""


class ConcurrentAppendError(VenueStoreError):
    """Another writer appended to the same account scope first."""


class SimulatedVenueInternalError(RuntimeError):
    """The simulator itself failed. Never an HTTP answer: a venue fault it is not."""

    kind = "bug"


class NoMarketDataError(SimulatedVenueInternalError):
    """No usable book (missing or stale) to freeze a queue position from."""

    kind = "no_market_data"


class FeedFailureError(SimulatedVenueInternalError):
    """The market feed raised, or was too slow (it may not do I/O inside the venue lock)."""

    kind = "feed"


@dataclass(frozen=True, slots=True)
class InternalFailure:
    """One simulator-internal failure; soak and CI count these separately from venue faults."""

    kind: str  # "no_market_data" | "feed" | "store" | "bug"
    detail: str


@dataclass(frozen=True, slots=True)
class SimAccount:
    """One simulated venue account: `(exchange_account_id, deployment_environment)`."""

    exchange_account_id: str
    deployment_environment: str

    def __post_init__(self) -> None:
        if not self.exchange_account_id:
            raise ValueError("simulated account needs an exchange_account_id")
        if self.deployment_environment not in ALLOWED_REALMS:
            raise RealmRefusedError(
                f"simulated venue refuses realm {self.deployment_environment!r}; "
                f"allowed: {ALLOWED_REALMS}"
            )


@dataclass(frozen=True, slots=True)
class PublicTrade:
    """One public funding trade: `[ID, MTS, AMOUNT, RATE, PERIOD]` reduced to what fills need."""

    mts: int
    amount: Decimal
    rate: Decimal
    period: int


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """Ask side of the public book: `(rate, period, amount)` per level."""

    symbol: str
    captured_at_ms: int
    asks: tuple[tuple[Decimal, int, Decimal], ...]


class MarketFeed(Protocol):
    """The venue's own market input. P1a has fixtures only; the live feed is P2.

    Contract for every implementation:

    - Both methods are non-blocking reads of data already held locally. They run
      inside the venue's request lock, so they must not do network I/O: a live feed
      polls public data on its own task and only fills an in-memory buffer. The
      venue enforces this with `SimulatedVenueConfig.feed_deadline_s`; a call that
      exceeds it is an internal failure, not a venue answer.
    - `book` returns the latest valid snapshot at or before `at_ms`. A snapshot
      older than `SimulatedVenueConfig.max_book_age_ms` is not usable: a submit then
      ends in the explicit "no market data" outcome instead of freezing a queue
      position from a stale book.
    """

    async def trades(
        self, symbol: str, *, after_ms: int, through_ms: int,
    ) -> Sequence[PublicTrade]:
        """Public trades with `after_ms < mts <= through_ms`, any order."""
        ...

    async def book(self, symbol: str, *, at_ms: int) -> BookSnapshot | None:
        """The latest valid snapshot taken at or before `at_ms`, or None."""
        ...


class VenueEventStore(Protocol):
    """Append-only event log per account scope; one shape for memory and SQL."""

    async def authority_epoch(self) -> str:
        """The raw latest authority epoch of the database this store writes to.

        Read by the store itself, on its own connection; a caller never supplies it.
        """
        ...

    async def load(self, account: SimAccount) -> Sequence[VenueEvent]:
        """All events of the scope, ordered by position."""
        ...

    async def append(
        self, account: SimAccount, expected_seq: int, events: Sequence[VenueEvent],
    ) -> None:
        """Append iff exactly `expected_seq` events exist; else raise ConcurrentAppendError."""
        ...


HistoryField = Literal["create", "update"]


@dataclass(frozen=True, slots=True)
class HistoryFilter:
    """Which timestamp each `/hist` stream filters and pages on.

    Bitfinex does not document this. Defaults are the client's assumption
    (offers by MTS_CREATE, credits and loans by MTS_UPDATE; trades have a single
    timestamp); tests also run the inverse to see whether the ledger stays sound
    or fails closed.
    """

    offers: HistoryField = "create"
    credits: HistoryField = "update"
    loans: HistoryField = "update"


@dataclass(frozen=True, slots=True)
class SimulatedVenueConfig:
    api_key: str
    api_secret: str
    symbols: tuple[str, ...] = ("fUST", "fUSD")
    fee_rate: Decimal = Decimal("0.15")
    min_offer_amount: Decimal = Decimal("150")
    min_period_days: int = 2
    max_period_days: int = 120
    payout_offset_ms: int = 5_400_000  # 01:30Z, Bitfinex's daily interest payout
    history_lag_ms: int = 0
    history_filter: HistoryFilter = field(default_factory=HistoryFilter)
    history_max_limit: int = 500
    # Bitfinex stamps offers to the whole second (observed by the client).
    whole_second_offer_mts: bool = True
    # "5xx": Bitfinex's real business rejection (5xx + ["error", ...]) which a
    # client must read as UNKNOWN. "200": the documented ERROR notification.
    business_rejection: Literal["5xx", "200"] = "5xx"
    # Same two shapes for "cancel of an offer that is not active". The real shape is
    # unconfirmed; default is Bitfinex's documented error shape (5xx + error array).
    cancel_rejection: Literal["5xx", "200"] = "5xx"
    max_book_age_ms: int = 120_000  # older snapshots give "no market data" on submit
    feed_deadline_s: float = 0.5  # a feed read slower than this is an internal failure
    # None: fills stay in loans until expiry. Otherwise a borrower draws the
    # loan into a credit after this delay (the row moves, opening is kept).
    draw_after_ms: int | None = None
    id_base: int = 40_000_000

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_secret:
            raise ValueError("simulated venue needs a synthetic api key and secret")
        if not self.symbols or any(_SYMBOL.match(s) is None for s in self.symbols):
            raise ValueError(f"invalid symbols: {self.symbols!r}")
        if not Decimal(0) <= self.fee_rate < Decimal(1):
            raise ValueError("fee_rate must be in [0, 1)")
        if self.min_offer_amount <= 0 or not 1 <= self.min_period_days <= self.max_period_days:
            raise ValueError("invalid offer limits")
        if not 0 <= self.payout_offset_ms < 86_400_000 or self.history_lag_ms < 0:
            raise ValueError("invalid payout offset or history lag")
        if not 1 <= self.history_max_limit <= 500:
            raise ValueError("history_max_limit must be in [1, 500]")
        if self.draw_after_ms is not None and self.draw_after_ms < 0:
            raise ValueError("draw_after_ms must be non-negative")
        if self.max_book_age_ms <= 0 or self.feed_deadline_s <= 0:
            raise ValueError("max_book_age_ms and feed_deadline_s must be positive")


class FaultTarget(StrEnum):
    SUBMIT = "submit"
    CANCEL = "cancel"
    CANCEL_ALL = "cancel_all"
    HISTORY = "history"
    ANY_REQUEST = "any_request"  # counts every request the transport receives


class FaultKind(StrEnum):
    # Applied and durable, but the response never arrives: the real "placed but lost".
    UNKNOWN_PLACED_LOST = "unknown_placed_lost"
    # The request never took effect and the caller sees a transport error.
    UNKNOWN_NOT_PLACED_LOST = "unknown_not_placed_lost"
    # Not applied; Bitfinex's real rejection shape: HTTP 500 + ["error", CODE, MESSAGE].
    UNKNOWN_5XX_ERROR = "unknown_5xx_error"
    # Not applied; the documented 200 notification with STATUS "ERROR".
    REJECTED = "rejected"
    # History pages silently lack their newest row (the client sees a complete page).
    HISTORY_OMIT_NEWEST = "history_omit_newest"
    # History read answers HTTP 503 (the client certifies nothing: coverage incomplete).
    HISTORY_ERROR = "history_error"
    # After the n-th request: run the rule's hook (tests push market data or move the
    # injected clock there), then let the venue catch up. The world moves between two
    # requests of one caller, as on a real venue.
    TICK_AFTER = "tick_after"


_HISTORY_KINDS = {FaultKind.HISTORY_OMIT_NEWEST, FaultKind.HISTORY_ERROR}


@dataclass(frozen=True, slots=True)
class FaultRule:
    """Fires on the n-th request of `target` (1-based) or with a seeded probability."""

    target: FaultTarget
    kind: FaultKind
    ordinals: frozenset[int] = frozenset()
    probability: float = 0.0
    # Only for TICK_AFTER; excluded from equality so a plan stays comparable data.
    hook: Callable[[], None] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if (self.target is FaultTarget.HISTORY) != (self.kind in _HISTORY_KINDS):
            raise ValueError(f"fault {self.kind} cannot target {self.target}")
        if (self.target is FaultTarget.ANY_REQUEST) != (self.kind is FaultKind.TICK_AFTER):
            raise ValueError("TICK_AFTER, and only it, targets ANY_REQUEST")
        if self.hook is not None and self.kind is not FaultKind.TICK_AFTER:
            raise ValueError("only TICK_AFTER takes a hook")
        if not 0.0 <= self.probability <= 1.0 or any(n < 1 for n in self.ordinals):
            raise ValueError("fault ordinals are 1-based and probability is in [0, 1]")
        if not self.ordinals and self.probability == 0.0:
            raise ValueError("a fault rule needs ordinals or a probability")


@dataclass(frozen=True, slots=True)
class FaultPlan:
    """Fault injection. Empty (off) by default; CI opts in."""

    rules: tuple[FaultRule, ...] = ()
    seed: int = 0
