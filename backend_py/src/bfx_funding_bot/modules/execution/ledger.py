"""PaperPositionLedger — event-sourcing replay from Axiom (Phase 4.3 dual counter).

Axiom is SoT (30d retention). On daemon startup, replay event stream
(RESERVATION_CLAIMED / ORDER_FILL / RESERVATION_RELEASED) to rebuild
in-memory counters. Subscribe to DomainEventBus for live updates.

reserved: open reservations (offer placed, no match yet). AllocationCap
uses reserved+realized for pre-trade reservation check.
realized: matched credits (actual exposure earning APR). L2 guards
(DrawdownGuard / DivergenceRateGuard, Phase 4.4) use realized only.

floor-at-0 on RELEASE without prior CLAIM: 30d retention edge case where
CLAIMED out of window but RELEASED in window. Tracked via
replay_floor_hit_count; pre-prod CI expects 0.

Axiom unreachable at boot → LedgerReplayError → daemon never starts.
Empty-ledger fallback is NOT acceptable — it would let AllocationCap
permit over-cap exposure (real safety hole).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from bfx_funding_bot.core.errors import LedgerReplayError
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    EventType,
    OrderFillPayload,
    ReservationClaimedPayload,
    ReservationReleasedPayload,
)

log = logging.getLogger(__name__)

_REPLAY_DEFAULT_RETRIES = 3
_REPLAY_BACKOFF_BASE_S = 1.0


class _AxiomQueryProtocol(Protocol):
    async def query_order_events(
        self, account_id: str, since: datetime,
    ) -> list[dict[str, Any]]: ...


class PaperPositionLedger:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self._reserved = Decimal("0")
        self._realized = Decimal("0")
        self.replay_floor_hit_count = 0

    # ---------- live update handlers (DomainEventBus subscribers) ----------

    async def on_reservation_claimed(self, event: ReservationClaimed) -> None:
        if event.account_id != self.account_id:
            return
        self._reserved += event.size_usdt

    async def on_order_filled(self, event: OrderFilled) -> None:
        if event.account_id != self.account_id:
            return
        delta = min(self._reserved, event.size_usdt)
        self._reserved -= delta
        if delta < event.size_usdt:
            self.replay_floor_hit_count += 1
            log.warning(
                "order_filled_without_claim cid=%d offer=%s expected=%.2f applied=%.2f",
                event.cid, event.venue_offer_id,
                float(event.size_usdt), float(delta),
            )
        self._realized += event.size_usdt

    async def on_reservation_released(self, event: ReservationReleased) -> None:
        if event.account_id != self.account_id:
            return
        delta = min(self._reserved, event.size_usdt)
        self._reserved -= delta
        if delta < event.size_usdt:
            self.replay_floor_hit_count += 1
            log.warning(
                "reservation_release_without_claim cid=%d offer=%s expected=%.2f applied=%.2f reason=%s",
                event.cid, event.venue_offer_id,
                float(event.size_usdt), float(delta), event.reason,
            )

    # ---------- public getters ----------

    def current_exposure(self) -> Decimal:
        """For AllocationCapGuard: reserved + realized = capital committed at venue."""
        return self._reserved + self._realized

    def realized_exposure(self) -> Decimal:
        """For L2 guards (DrawdownGuard etc., Phase 4.4): matched credits only."""
        return self._realized

    # ---------- replay (Task 5 will refactor to event-type dispatch) ----------

    @classmethod
    async def replay_from_axiom(
        cls,
        *,
        account_id: str,
        since: datetime,
        axiom_query: _AxiomQueryProtocol,
        max_retries: int = _REPLAY_DEFAULT_RETRIES,
    ) -> PaperPositionLedger:
        last_exc: BaseException | None = None
        for attempt in range(1, max_retries + 1):
            try:
                events = await axiom_query.query_order_events(account_id, since)
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries:
                    backoff = _REPLAY_BACKOFF_BASE_S * (2 ** (attempt - 1))
                    log.warning(
                        "ledger_replay_attempt_failed attempt=%d/%d backoff=%.1fs err=%r",
                        attempt, max_retries, backoff, exc,
                    )
                    await asyncio.sleep(backoff)
                continue
            break
        else:
            raise LedgerReplayError(
                f"axiom replay exhausted {max_retries} attempts: {last_exc!r}"
            )

        ledger = cls(account_id=account_id)
        # Defensive sort (trust upstream contract + verify): protocol docstring
        # says "should return chronological", consumer sorts to tolerate regression.
        events_sorted = sorted(events, key=lambda e: e.get("_time", ""))
        for ev in events_sorted:
            event_type = ev.get("event_type")
            if ev.get("account_id") != account_id:
                continue
            try:
                if event_type == EventType.RESERVATION_CLAIMED.value:
                    payload_c = ReservationClaimedPayload.model_validate(ev["payload"])
                    await ledger.on_reservation_claimed(ReservationClaimed(
                        cid=payload_c.cid, venue_offer_id=payload_c.venue_offer_id,
                        size_usdt=Decimal(str(payload_c.size_usdt)),
                        signal_correlation_id=payload_c.signal_correlation_id,
                        account_id=account_id, is_simulated=payload_c.is_simulated,
                    ))
                elif event_type == EventType.ORDER_FILL.value:
                    payload_f = OrderFillPayload.model_validate(ev["payload"])
                    await ledger.on_order_filled(OrderFilled(
                        cid=payload_f.cid, venue_offer_id=payload_f.offer_id,
                        credit_id=None,
                        size_usdt=Decimal(str(payload_f.fill_size_usdt)),
                        fill_rate=payload_f.fill_price,
                        signal_correlation_id=payload_f.signal_correlation_id,
                        account_id=account_id, is_simulated=payload_f.is_simulated,
                    ))
                elif event_type == EventType.RESERVATION_RELEASED.value:
                    payload_r = ReservationReleasedPayload.model_validate(ev["payload"])
                    await ledger.on_reservation_released(ReservationReleased(
                        cid=payload_r.cid, venue_offer_id=payload_r.venue_offer_id,
                        size_usdt=Decimal(str(payload_r.size_usdt)),
                        reason=payload_r.reason,
                        signal_correlation_id=payload_r.signal_correlation_id,
                        account_id=account_id, is_simulated=payload_r.is_simulated,
                    ))
                # else: any other event_type (DECISION, SAFETY_TRIGGER, etc.) is
                # not a ledger event — skip silently.
            except Exception as exc:
                log.warning(
                    "ledger_replay_skip_malformed event_type=%s err=%r payload=%r",
                    event_type, exc, ev.get("payload"),
                )
        log.info(
            "ledger_replay_complete account=%s events=%d reserved=%s realized=%s floor_hits=%d",
            account_id, len(events_sorted), ledger._reserved, ledger._realized,
            ledger.replay_floor_hit_count,
        )
        return ledger

