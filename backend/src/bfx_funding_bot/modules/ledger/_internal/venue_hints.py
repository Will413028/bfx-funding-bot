"""Dormant venue hints: reconciliation owns truth, never WS/REST deltas.

Resync requests use a five-second leading-edge debounce per (kind, venue ID).
The fixed window uses local monotonic receipt time, never venue timestamps or
sequence numbers. All hints still publish non-authoritative notifications.
Expired keys are pruned on receipt; no task/timer or database access is needed.
"""

from collections.abc import Callable

from bfx_funding_bot.modules.ledger import (
    CreditCloseHint,
    OfferCloseHint,
    Scope,
    VenueHintNotification,
    VenueHintPublisher,
)

RESYNC_DEBOUNCE_SECONDS = 5.0


class LedgerVenueHintSink:
    def __init__(
        self, *, scope: Scope, request_resync: Callable[[str], None],
        bus: VenueHintPublisher, monotonic: Callable[[], float],
    ) -> None:
        self._scope = scope
        self._request_resync = request_resync
        self._bus = bus
        self._monotonic = monotonic
        self._requested_at: dict[tuple[str, str], float] = {}

    async def _notify(self, notification: VenueHintNotification) -> None:
        now = self._monotonic()
        self._requested_at = {
            key: requested for key, requested in self._requested_at.items()
            if now - requested < RESYNC_DEBOUNCE_SECONDS
        }
        venue_id = (notification.venue_offer_id if notification.venue_offer_id is not None
                    else str(notification.credit_id))
        key = (notification.kind, venue_id)
        if key not in self._requested_at:
            # Synchronous callback, just like PeriodicReconcile.request_resync.
            # Do not mark a failed callback as successfully requested.
            self._request_resync(f"venue_hint:{notification.kind}:{venue_id}")
            self._requested_at[key] = now
        await self._bus.publish(notification)

    async def offer_closed(self, hint: OfferCloseHint) -> None:
        await self._notify(VenueHintNotification(
            scope=self._scope, kind="offer_closed", venue_offer_id=hint.venue_offer_id,
            occurred_at_ms=hint.occurred_at_ms, venue_seq=hint.venue_seq, status=hint.kind,
        ))

    async def credit_closed(self, hint: CreditCloseHint) -> None:
        await self._notify(VenueHintNotification(
            scope=self._scope, kind="credit_closed", credit_id=hint.credit_id,
            occurred_at_ms=hint.occurred_at_ms, venue_seq=hint.venue_seq,
        ))

    async def offer_gone(self, venue_offer_id: str, *, occurred_at_ms: int) -> bool:
        await self._notify(VenueHintNotification(
            scope=self._scope, kind="offer_gone", venue_offer_id=venue_offer_id,
            occurred_at_ms=occurred_at_ms,
        ))
        return True
