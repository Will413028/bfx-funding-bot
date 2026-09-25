"""ReconcileNavTracker — per-symbol NAV source for the L2 loss-limiter guards.

NAV (account equity) for a currency is sampled from each per-symbol
PositionReconciled venue snapshot:

    NAV = available + reserved + realized

i.e. total funding-wallet capital for that currency — idle funds + open offers +
lent principal. A maturing credit returns principal to `available` so NAV is
unchanged; interest paid raises `available` → NAV up; capital lost for ANY reason
(a bug burning funds, a platform socialised loss, a withdrawal) → NAV down.

Metrics are PER SYMBOL — each currency is measured against its OWN history and
NEVER summed across currencies (native units differ, and a profitable currency
must never mask a losing one — the per-currency risk-isolation invariant). For
each symbol:

  * realized_loss_pct_24h(symbol) = (highest NAV in last 24h − latest NAV) / that
      high × 100 — catches fast recent bleeding in that currency.
  * drawdown_pct(symbol)          = (all-time peak NAV − latest NAV) / all-time
      peak × 100 — catches slow sustained decline from that currency's peak.

Both are PERCENTAGES so the guards auto-scale with funded capital — no manual
re-anchoring on deposit/withdrawal. With a single active symbol (fUST today) a
symbol's metric is identical to the pre-per-symbol scalar tracker.

Each symbol's 24h window is trimmed against that symbol's latest occurred_at_ms
(the reconcile clock), so the source needs no wall-clock injection and is fully
deterministic from the event stream.

The 24h window persists via `window_store` (nav_window_samples, T9): a sample
is written when a symbol's NAV changes or every few minutes, and the window is
reloaded at boot, so a restart right after a loss no longer rebuilds the window
from the post-loss NAV and reads zero. Without a store the window is in-memory
only, as before. The all-time peak optionally persists via `peak_store`
(nav_peak table): loaded at boot (max-merged with any live samples), saved on
every new high-water mark. Persistence failures are logged and swallowed — the
reconcile money-path must never depend on this table. With peak_store=None the
tracker keeps the original fully in-memory behavior.

Caveat: a manual withdrawal lowers a currency's NAV and so reads as a drawdown
for that currency — for a single-operator canary, halting that currency's trading
on an unexplained equity drop is the desired behaviour.
"""
from __future__ import annotations

import logging
from collections import deque
from decimal import Decimal
from typing import Protocol

from bfx_funding_bot.modules.execution.events import PositionReconciled

log = logging.getLogger(__name__)

_WINDOW_MS = 24 * 60 * 60 * 1000


class _PeakStore(Protocol):
    async def load(self) -> dict[str, Decimal]: ...
    async def save(self, symbol: str, peak: Decimal, updated_at_ms: int) -> None: ...


class _WindowStore(Protocol):
    async def load(self) -> dict[str, list[tuple[int, Decimal]]]: ...
    async def save(self, symbol: str, occurred_at_ms: int, nav: Decimal) -> None: ...


# Persist an unchanged NAV at least this often, so the stored window keeps up.
_WINDOW_HEARTBEAT_MS = 10 * 60 * 1000


class ReconcileNavTracker:
    def __init__(self, account_id: str, *, peak_store: _PeakStore | None = None,
                 window_store: _WindowStore | None = None) -> None:
        self.account_id = account_id
        self._peak_store = peak_store
        self._window_store = window_store
        self._last_persisted: dict[str, tuple[int, Decimal]] = {}
        # Per-symbol all-time peak NAV (high-water mark). Keyed by symbol so a
        # profitable currency never lifts another currency's peak.
        self._peak_by_symbol: dict[str, Decimal] = {}
        # Per-symbol 24h window of (occurred_at_ms, nav), oldest first, each
        # trimmed against its own latest occurred_at_ms.
        self._samples_by_symbol: dict[str, deque[tuple[int, Decimal]]] = {}

    async def load_persisted_peaks(self) -> None:
        """Seed peaks from the store at boot. Max-merge: a stale persisted peak
        never lowers a peak already observed live this process."""
        if self._peak_store is None:
            return
        try:
            persisted = await self._peak_store.load()
        except Exception:
            log.exception("nav_peak_load_failed — starting with in-memory peaks only")
            return
        for symbol, peak in persisted.items():
            live = self._peak_by_symbol.get(symbol)
            if live is None or peak > live:
                self._peak_by_symbol[symbol] = peak

    async def load_persisted_window(self) -> None:
        """Seed each symbol's 24h window from the store at boot (merge, never replace)."""
        if self._window_store is None:
            return
        try:
            persisted = await self._window_store.load()
        except Exception:
            log.exception("nav_window_load_failed — starting with an empty 24h window")
            return
        for symbol, rows in persisted.items():
            merged = sorted({*self._samples_by_symbol.get(symbol, ()), *rows})
            self._samples_by_symbol[symbol] = deque(merged)
            if rows:
                self._last_persisted[symbol] = rows[-1]

    async def _persist_sample(self, symbol: str, occurred_at_ms: int, nav: Decimal) -> None:
        if self._window_store is None:
            return
        last = self._last_persisted.get(symbol)
        if last is not None and last[1] == nav and occurred_at_ms - last[0] < _WINDOW_HEARTBEAT_MS:
            return
        try:
            await self._window_store.save(symbol, occurred_at_ms, nav)
            self._last_persisted[symbol] = (occurred_at_ms, nav)
        except Exception:
            # Never break the reconcile path; worst case the window after a
            # restart misses this sample.
            log.exception("nav_window_save_failed symbol=%s", symbol)

    async def on_position_reconciled(self, event: PositionReconciled) -> None:
        if event.account_id != self.account_id:
            return
        # _resolve_position_fields guarantees these are non-None at runtime.
        available = event.available
        reserved = event.reserved
        realized = event.realized
        assert available is not None and reserved is not None and realized is not None
        symbol = event.symbol
        nav = available + reserved + realized  # native units; never cross-summed
        samples = self._samples_by_symbol.setdefault(symbol, deque())
        samples.append((event.occurred_at_ms, nav))
        await self._persist_sample(symbol, event.occurred_at_ms, nav)
        peak = self._peak_by_symbol.get(symbol)
        if peak is None or nav > peak:
            self._peak_by_symbol[symbol] = nav
            if self._peak_store is not None:
                try:
                    await self._peak_store.save(symbol, nav, event.occurred_at_ms)
                except Exception:
                    # Persistence must never break the reconcile path; worst
                    # case the peak regresses to the last saved value on restart.
                    log.exception("nav_peak_save_failed symbol=%s", symbol)
        cutoff = event.occurred_at_ms - _WINDOW_MS
        while samples and samples[0][0] < cutoff:
            samples.popleft()

    # ---- _PnLSourceProtocol (calibrated_guards) ----

    def realized_loss_pct_24h(self, symbol: str) -> float:
        samples = self._samples_by_symbol.get(symbol)
        if not samples:
            return 0.0
        latest_nav = samples[-1][1]
        window_high = max(nav for _, nav in samples)
        if window_high <= 0:
            return 0.0
        loss = max(Decimal("0"), window_high - latest_nav)
        return float(loss / window_high * 100)

    def drawdown_pct(self, symbol: str) -> float:
        peak = self._peak_by_symbol.get(symbol)
        samples = self._samples_by_symbol.get(symbol)
        if peak is None or peak <= 0 or not samples:
            return 0.0
        latest_nav = samples[-1][1]
        return float((peak - latest_nav) / peak * 100)
