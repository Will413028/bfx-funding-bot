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

All in-memory (mirrors PaperPositionLedger.available_balance's deliberate
no-persistence design): every symbol's peak and 24h window reset on restart and
rebuild within ~90s of that symbol's first reconcile. Before a symbol's first
reconcile its metrics return 0.0 (permissive). Limitation: a drawdown developing
across a restart is forgotten; persisting per-symbol peaks is a separate
follow-up.

Caveat: a manual withdrawal lowers a currency's NAV and so reads as a drawdown
for that currency — for a single-operator canary, halting that currency's trading
on an unexplained equity drop is the desired behaviour.
"""
from __future__ import annotations

from collections import deque
from decimal import Decimal

from bfx_funding_bot.modules.execution.events import PositionReconciled

_WINDOW_MS = 24 * 60 * 60 * 1000


class ReconcileNavTracker:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        # Per-symbol all-time peak NAV (high-water mark). Keyed by symbol so a
        # profitable currency never lifts another currency's peak.
        self._peak_by_symbol: dict[str, Decimal] = {}
        # Per-symbol 24h window of (occurred_at_ms, nav), oldest first, each
        # trimmed against its own latest occurred_at_ms.
        self._samples_by_symbol: dict[str, deque[tuple[int, Decimal]]] = {}

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
        peak = self._peak_by_symbol.get(symbol)
        if peak is None or nav > peak:
            self._peak_by_symbol[symbol] = nav
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
