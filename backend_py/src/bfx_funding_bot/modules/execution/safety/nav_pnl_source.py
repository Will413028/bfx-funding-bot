"""ReconcileNavTracker — NAV-based source for the L2 loss-limiter guards.

Replaces _StubPnLSource (returned 0/0, making RealizedLossGuard + DrawdownGuard
no-ops on the real-money canary even though the canary invariant forces them on).

NAV (account equity) is sampled from each PositionReconciled venue snapshot:

    NAV = available + reserved + realized

i.e. total funding-wallet capital for the account's currency — idle funds + open
offers + lent principal. With multiple active symbols the global NAV sample is
the SUM of the latest per-symbol NAV (native units kept separate per bucket; the
sum is a pure equity total, never used as a per-symbol cap). A single active
symbol (fUST today) makes the global metrics identical to the scalar tracker. A
maturing credit returns principal to `available` so NAV is unchanged; interest
paid raises `available` → NAV up; capital lost for ANY reason (a bug burning
funds, a platform socialised loss, a withdrawal) → NAV down.

Two metrics over two horizons (both fed to the guards synchronously):

  * realized_loss_pct_24h() = (highest NAV in last 24h − latest NAV) / that high × 100
      catches fast recent bleeding. A PERCENTAGE so the guard auto-scales with
      funded capital — no manual re-anchoring on deposit/withdrawal.
  * drawdown_pct()      = (all-time peak NAV − latest NAV) / all-time peak × 100
      catches slow sustained decline from the high-water mark.

The 24h window is trimmed against the latest sample's occurred_at_ms (the
reconcile clock), so the source needs no wall-clock injection and is fully
deterministic from the event stream.

All in-memory (mirrors PaperPositionLedger.available_balance's deliberate
no-persistence design): the peak and the 24h window reset on restart and rebuild
within ~90s of the first reconcile. Before the first reconcile NAV is unknown, so
both metrics return 0 (permissive). Limitation: a drawdown developing across a
restart is forgotten; persisting the peak is a follow-up if it proves necessary.

Caveat: a manual withdrawal lowers NAV and so reads as a drawdown — for a
single-operator canary, halting trading on an unexplained equity drop is the
desired behaviour.
"""
from __future__ import annotations

from collections import deque
from decimal import Decimal

from bfx_funding_bot.modules.execution.events import PositionReconciled

_WINDOW_MS = 24 * 60 * 60 * 1000


class ReconcileNavTracker:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self._peak: Decimal | None = None
        # (occurred_at_ms, global_nav), oldest first, trimmed to the 24h window.
        # global_nav = Σ over the latest-known NAV of every symbol seen so far.
        self._samples: deque[tuple[int, Decimal]] = deque()
        # Latest NAV component per symbol; summed to form each global sample.
        # Single active symbol today (fUST) ⇒ one bucket ⇒ global == that bucket.
        self._nav_by_symbol: dict[str, Decimal] = {}

    async def on_position_reconciled(self, event: PositionReconciled) -> None:
        if event.account_id != self.account_id:
            return
        # _resolve_position_fields guarantees these are non-None at runtime.
        # Narrow from Decimal | None so mypy accepts the arithmetic.
        available = event.available
        reserved = event.reserved
        realized = event.realized
        assert available is not None and reserved is not None and realized is not None
        self._nav_by_symbol[event.symbol] = available + reserved + realized
        # Global NAV sample = Σ latest-per-symbol NAV. A single active symbol
        # makes this identical to the pre-per-symbol scalar NAV.
        nav = sum(self._nav_by_symbol.values(), Decimal("0"))
        self._samples.append((event.occurred_at_ms, nav))
        if self._peak is None or nav > self._peak:
            self._peak = nav
        cutoff = event.occurred_at_ms - _WINDOW_MS
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    # ---- _PnLSourceProtocol (calibrated_guards) ----

    def realized_loss_pct_24h(self) -> float:
        if not self._samples:
            return 0.0
        latest_nav = self._samples[-1][1]
        window_high = max(nav for _, nav in self._samples)
        if window_high <= 0:
            return 0.0
        loss = max(Decimal("0"), window_high - latest_nav)
        return float(loss / window_high * 100)

    def drawdown_pct(self) -> float:
        if self._peak is None or self._peak <= 0:
            return 0.0
        latest_nav = self._samples[-1][1]
        return float((self._peak - latest_nav) / self._peak * 100)
