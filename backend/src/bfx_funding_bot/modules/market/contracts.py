"""Public market contracts: backfill results and the live funding book."""
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol

from bfx_funding_bot.external.bitfinex.funding_book_level import FundingBookLevel


@dataclass(frozen=True)
class SeriesSpec:
    """Identifies one backfill series.

    For kind="candles": (symbol, timeframe, period_agg) all required.
    For kind="funding_stats": only symbol used.
    """
    kind: Literal["candles", "funding_stats"]
    symbol: str
    timeframe: str | None = None
    period_agg: str | None = None

    def label(self) -> str:
        if self.kind == "candles":
            return f"candles {self.symbol} {self.timeframe} {self.period_agg}"
        return f"funding_stats {self.symbol}"


@dataclass(frozen=True)
class BackfillStats:
    """Successful backfill result for one series."""
    spec: SeriesSpec
    pages: int
    rows: int           # total rows upserted (may include resume-from-DB duplicates)
    earliest_mts: int   # smallest mts now in DB for this series after run


@dataclass(frozen=True)
class BackfillError:
    """Failed backfill for one series — orchestrator continues with others."""
    spec: SeriesSpec
    error: str          # repr / brief summary of exception


class BackfillCursorStuck(Exception):  # noqa: N818
    """Walking-back loop's cursor failed to advance.

    Bitfinex returned a page whose oldest mts is >= our current end_ms,
    which would cause an infinite loop. Most likely cause: API behaviour
    changed (no longer strictly < end_ms). Service raises this rather
    than silently looping.
    """

    def __init__(self, symbol: str, end_ms: int, oldest_mts: int) -> None:
        super().__init__(
            f"cursor stuck: symbol={symbol} end_ms={end_ms} "
            f"oldest_mts={oldest_mts} (expected oldest < end)"
        )
        self.symbol = symbol
        self.end_ms = end_ms
        self.oldest_mts = oldest_mts


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class ExactPeriodBook:
    bids: tuple[FundingBookLevel, ...]
    asks: tuple[FundingBookLevel, ...]


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    snapshot_id: str
    symbol: str
    bids: tuple[FundingBookLevel, ...]
    asks: tuple[FundingBookLevel, ...]
    captured_at_ms: int
    received_at_ms: int
    source: Literal["ws", "rest_reconciled"]
    # Both assert the absence of a detected inconsistency, not the presence of
    # a proof. A book the venue handed over whole is consistent until the venue
    # says otherwise: it numbers every frame, so a gap is visible, and it sends
    # a checksum on its own schedule -- tens of seconds apart, sometimes over
    # two minutes. Waiting for that proof before pricing would idle the book
    # for most of its life while nothing was ever wrong with it.
    sequence_valid: bool
    checksum_valid: bool
    sequence: int | None
    # Bound by the provider that admitted this exact snapshot, never a fresh
    # unrelated book substituted after pricing. Missing evidence cannot send.
    max_age_ms: int | None = None

    def is_fresh(self, *, symbol: str, now_ms: int, max_age_ms: int) -> bool:
        """Age the book by when the venue last confirmed it, not by its content.

        A quiet funding book can go minutes without a level changing while the
        venue keeps confirming, per heartbeat, that nothing has. Measuring from
        `captured_at_ms` would retire such a book for being correct and
        unchanged; `received_at_ms` measures the thing that actually decays --
        how long since the venue last spoke to us about it.
        """
        return (
            symbol == self.symbol
            # Neither timestamp may sit in the future: that is a broken clock,
            # not a fresh book.
            and self.captured_at_ms <= now_ms
            and self.received_at_ms <= now_ms
            and now_ms - self.received_at_ms <= max_age_ms
            and self.sequence_valid
            and self.checksum_valid
        )

    def exact_period(self, *, period_days: int, amount: Decimal) -> ExactPeriodBook | None:
        bids = tuple(level for level in self.bids if level.period == period_days)
        asks = tuple(level for level in self.asks if level.period == period_days)
        bid_depth = sum((-_decimal(level.amount) for level in bids), Decimal("0"))
        ask_depth = sum((_decimal(level.amount) for level in asks), Decimal("0"))
        if (not bids and not asks) or max(bid_depth, ask_depth) < amount:
            return None
        return ExactPeriodBook(bids=bids, asks=asks)


class BookUnavailable(StrEnum):
    """Why a book cannot price right now, kept distinct for the operator.

    Collapsing these into one reason is what made the venue-checksum fault take
    a full investigation: the block said the book was stale when in truth it had
    never once qualified, and nothing in the event said which.
    """

    NO_BASELINE = "no_baseline"
    SEQUENCE_GAP = "sequence_gap"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    STALE = "stale"
    VENUE_MAINTENANCE = "venue_maintenance"


class FundingBookProvider(Protocol):
    def snapshot(self, symbol: str, *, now_ms: int) -> MarketSnapshot | None: ...

    def unavailable_reason(self, symbol: str, *, now_ms: int) -> BookUnavailable | None: ...
