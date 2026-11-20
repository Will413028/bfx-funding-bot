"""Replay a hypothetical funding offer against the recorded public trade tape.

The tape is the VM's daily files `<root>/<symbol>/<YYYY-MM-DD>.csv.gz`
(`id,mts,amount,rate,period`, signed amount, sorted by `(mts, id)`; written by
`scripts/ingest_public_trades.py`, ADR 2026-11-20). Files are read one day at a
time and only a few days stay cached: the whole fUSD history parsed at once
peaks near 8.7 GB RSS (trade-tape plan, spike report 1b section 3).

Which public trades can fill an offer of rate r, period P, placed at t0:

- strictly after t0 and within the wait window;
- borrower-initiated (negative amount): a borrower took resting offers;
- public PERIOD <= P. The public PERIOD may be the bid's or the offer's tenor
  (undecidable from our own fills, spike 1b section 1); this rule is exact in the
  first reading and conservative in the second;
- rate > r fills us by cumulative volume (price priority: a borrower paying more
  than r had to clear the cheaper asks first). Rate == r is queue-dependent:
  the conservative bound counts none of it, the optimistic bound all of it, and
  the queue estimate charges the caller-supplied same-rate queue ahead of us
  (the shared `queue_fill.filled_amount` rule, vectorised).

Known limitations, both carried into every replay report: FRR fills are not
distinguishable on the tape and are not excluded (optimistic bias, spike 1b
section 5); there is no market-impact model, which matters for fUST periods of
15 days and more where the daily p10 volume is 150-472 (spike 1b section 2).
"""
from __future__ import annotations

import gzip
import io
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import numpy.typing as npt

from bfx_funding_bot.modules.backtest.incomplete import BacktestIncomplete

_COL_MTS, _COL_AMOUNT, _COL_RATE, _COL_PERIOD = 1, 2, 3, 4
_EMPTY = np.empty((0, 5), dtype=np.float64)
_AMOUNT_QUANTUM = Decimal("0.00000001")

Tape = npt.NDArray[np.float64]


def _utc_day(mts: int) -> date:
    return datetime.fromtimestamp(mts / 1000, tz=UTC).date()


class TapeDays:
    """Day-file reader for one symbol with a small LRU cache of parsed days."""

    def __init__(self, root: Path, symbol: str, *, cache_days: int = 4) -> None:
        if cache_days < 1:
            raise ValueError("cache_days must be >= 1")
        self._folder = root / symbol
        self._cache_days = cache_days
        self._cache: OrderedDict[date, Tape] = OrderedDict()
        # The last concatenated multi-day span: consecutive hourly offers reuse it.
        self._span: tuple[tuple[date, date], Tape] | None = None

    def day(self, day: date) -> Tape:
        cached = self._cache.get(day)
        if cached is not None:
            self._cache.move_to_end(day)
            return cached
        path = self._folder / f"{day.isoformat()}.csv.gz"
        if not path.is_file():
            raise BacktestIncomplete("tape_day_missing")
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            text = stream.read()
        _header, _, body = text.partition("\n")
        tape = (np.loadtxt(io.StringIO(body), delimiter=",", dtype=np.float64, ndmin=2)
                if body.strip() else _EMPTY)
        self._cache[day] = tape
        while len(self._cache) > self._cache_days:
            self._cache.popitem(last=False)
        return tape

    def window(self, start_mts: int, end_mts: int) -> Tape:
        """Rows with start_mts < mts <= end_mts, across day files."""
        first, last = _utc_day(start_mts), _utc_day(end_mts)
        # Keep every day of the window cached so hourly offers do not re-parse.
        self._cache_days = max(self._cache_days, (last - first).days + 2)
        span = (first, last)
        if self._span is None or self._span[0] != span:
            parts = []
            current = first
            while current <= last:
                parts.append(self.day(current))
                current += timedelta(days=1)
            rows = np.concatenate(parts) if len(parts) > 1 else parts[0]
            self._span = (span, rows)
        rows = self._span[1]
        # Day files are sorted by (mts, id), so the window is one contiguous slice.
        mts = rows[:, _COL_MTS]
        lo = int(np.searchsorted(mts, start_mts, side="right"))
        hi = int(np.searchsorted(mts, end_mts, side="right"))
        return rows[lo:hi]

    def cached_days(self) -> int:
        return len(self._cache)


@dataclass(frozen=True)
class HypotheticalOffer:
    rate: Decimal
    period_days: int
    placed_mts: int
    amount: Decimal


@dataclass(frozen=True)
class FillPath:
    """One bound's outcome: amount filled within the window and when."""

    filled: Decimal
    first_fill_mts: int | None
    full_fill_mts: int | None


@dataclass(frozen=True)
class ReplayResult:
    conservative: FillPath
    optimistic: FillPath
    queue_estimate: FillPath | None


def _path(mts: npt.NDArray[np.float64], cumulative: npt.NDArray[np.float64],
          amount: Decimal) -> FillPath:
    if cumulative.size == 0 or cumulative[-1] <= 0:
        return FillPath(Decimal(0), None, None)
    total = Decimal(repr(float(cumulative[-1]))).quantize(_AMOUNT_QUANTUM)
    first = int(mts[int(np.argmax(cumulative > 0))])
    # Same tolerance as the 1e-8 rounding of `total`: float sums of 8-decimal
    # amounts can land a hair under the exact total.
    full_idx = np.flatnonzero(cumulative >= float(amount) - 1e-8)
    full = int(mts[int(full_idx[0])]) if full_idx.size else None
    return FillPath(min(total, amount), first, full)


def replay_offer(
    offer: HypotheticalOffer,
    tape: TapeDays,
    *,
    max_wait_ms: int,
    same_rate_queue_ahead: Decimal | None = None,
) -> ReplayResult:
    """Fill bounds for `offer` resting at most `max_wait_ms` after placement."""
    if offer.amount <= 0 or max_wait_ms <= 0:
        raise ValueError("offer amount and max_wait_ms must be positive")
    rows = tape.window(offer.placed_mts, offer.placed_mts + max_wait_ms)
    rate = float(offer.rate)
    eligible = rows[(rows[:, _COL_AMOUNT] < 0) & (rows[:, _COL_PERIOD] <= offer.period_days)
                    & (rows[:, _COL_RATE] >= rate * (1 - 1e-9))]
    mts = eligible[:, _COL_MTS]
    volume = -eligible[:, _COL_AMOUNT]
    same = np.isclose(eligible[:, _COL_RATE], rate, rtol=1e-9, atol=0.0)
    above_cum = np.cumsum(np.where(same, 0.0, volume))
    same_cum = np.cumsum(np.where(same, volume, 0.0))

    queue_estimate = None
    if same_rate_queue_ahead is not None:
        # Vectorised `queue_fill.filled_amount` with no cap: same-rate volume
        # reaches us only after the queue ahead at our rate has traded.
        through_queue = np.clip(same_cum - float(same_rate_queue_ahead), 0.0, None)
        queue_estimate = _path(mts, above_cum + through_queue, offer.amount)
    return ReplayResult(
        conservative=_path(mts, above_cum, offer.amount),
        optimistic=_path(mts, above_cum + same_cum, offer.amount),
        queue_estimate=queue_estimate,
    )
