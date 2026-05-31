"""ReconcileNavTracker — NAV-based source for the L2 loss-limiter guards.

Replaces _StubPnLSource (returned 0/0, making RealizedLossGuard + DrawdownGuard
no-ops on the real-money canary even though the canary invariant forces them on).

NAV (account equity) is sampled from each PositionReconciled venue snapshot:

    NAV = available + reserved + realized

i.e. total funding-wallet capital for the account's currency — idle funds + open
offers + lent principal. A maturing credit returns principal to `available` so
NAV is unchanged; interest paid raises `available` → NAV up; capital lost for ANY
reason (a bug burning funds, a platform socialised loss, a withdrawal) → NAV down.

Two metrics over two horizons (both fed to the guards synchronously):

  * realized_loss_24h() = max(0, highest NAV in last 24h − latest NAV)
      catches fast recent bleeding.
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
        # (occurred_at_ms, nav), oldest first, trimmed to the 24h window.
        self._samples: deque[tuple[int, Decimal]] = deque()

    async def on_position_reconciled(self, event: PositionReconciled) -> None:
        if event.account_id != self.account_id:
            return
        # _resolve_position_fields guarantees these are non-None at runtime
        assert event.available_usdt is not None
        assert event.reserved_usdt is not None
        assert event.realized_usdt is not None
        nav = event.available_usdt + event.reserved_usdt + event.realized_usdt
        self._samples.append((event.occurred_at_ms, nav))
        if self._peak is None or nav > self._peak:
            self._peak = nav
        cutoff = event.occurred_at_ms - _WINDOW_MS
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    # ---- _PnLSourceProtocol (calibrated_guards) ----

    def realized_loss_24h(self) -> Decimal:
        if not self._samples:
            return Decimal("0")
        latest_nav = self._samples[-1][1]
        window_high = max(nav for _, nav in self._samples)
        return max(Decimal("0"), window_high - latest_nav)

    def drawdown_pct(self) -> float:
        if self._peak is None or self._peak <= 0:
            return 0.0
        latest_nav = self._samples[-1][1]
        return float((self._peak - latest_nav) / self._peak * 100)
