"""Legacy VenueHintSink: byte-identical translation, persistence and fanout.

WS sequence dedup is durable, before fanout. REST disappearance retains its
original retry-on-persist-failure behaviour and ignores persistence statuses.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from bfx_funding_bot.external.bitfinex.auth_ws import (
    BfxWSEvent,
    FccEvent,
    FcnEvent,
    FcuEvent,
    FocEvent,
)
from bfx_funding_bot.modules.execution.event_store.persister import EventPersister
from bfx_funding_bot.modules.execution.events import CreditClosed, OrderFilled, ReservationReleased
from bfx_funding_bot.modules.execution.registry_offers import (
    ClaimRecord,
    DiagnosticLog,
    RegistryState,
    ReservationCorrelationError,
)
from bfx_funding_bot.modules.ledger import CreditCloseHint, OfferCloseHint

log = logging.getLogger(__name__)


class InvariantError(RuntimeError):
    """Polling tracker encountered an impossible legacy reservation state."""


USER_CANCEL_WINDOW_MS = 5_000


@dataclass(frozen=True, slots=True)
class RegistryMutation:
    venue_offer_id: str
    new_state: RegistryState
    occurred_at_ms: int


def translate_bfx_event(
    bfx_event: BfxWSEvent,
    snapshot: dict[str, ClaimRecord],
    recent_cancels: dict[str, int],
    now_ms: int,
    *,
    account_id: str | None = None,
) -> tuple[list[Any], list[RegistryMutation], list[DiagnosticLog]]:
    """Pure mapping. Returns (domain_events, mutations, diagnostics)."""
    if isinstance(bfx_event, FcnEvent):
        return [], [], []  # informational only — no offer id; foc EXECUTED is the fill signal
    if isinstance(bfx_event, FocEvent):
        return _translate_foc(OfferCloseHint(
            venue_offer_id=bfx_event.venue_offer_id, symbol=bfx_event.symbol,
            kind=bfx_event.status, rate=bfx_event.rate, occurred_at_ms=bfx_event.mts_update,
            venue_seq=bfx_event.raw_seq, received_at_ms=now_ms,
            cancel_requested_at_ms=recent_cancels.get(bfx_event.venue_offer_id),
        ), snapshot)
    if isinstance(bfx_event, FccEvent):
        if account_id is None:
            raise ValueError("account_id is required to persist a credit-close event")
        # Audit-only release truth for attribution (no offer linkage on the
        # venue credit object, no registry mutation, zero ledger effect).
        closed = CreditClosed(
            symbol=bfx_event.symbol,
            credit_id=bfx_event.credit_id,
            amount=bfx_event.amount,
            rate=bfx_event.rate,
            period_days=bfx_event.period_days,
            mts_create=bfx_event.mts_create,
            account_id=account_id,
            is_simulated=False,
            venue_seq=bfx_event.raw_seq,
            occurred_at_ms=(bfx_event.mts_last_payout
                            if bfx_event.mts_last_payout is not None else bfx_event.mts_update),
            mts_opening=bfx_event.mts_opening,
            mts_last_payout=bfx_event.mts_last_payout,
        )
        return [closed], [], []
    if isinstance(bfx_event, FcuEvent):
        return [], [], []  # 4.4a: rate updates not modeled
    return [], [], []  # Heartbeat / AuthAck / ChannelInfo / Unknown


def _translate_foc(
    foc: OfferCloseHint,
    snapshot: dict[str, ClaimRecord],
) -> tuple[list[Any], list[RegistryMutation], list[DiagnosticLog]]:
    voi = foc.venue_offer_id
    claim = snapshot.get(voi)

    if claim is None:
        # No claim: a foreign offer (manual, auto-renew; ARCHITECTURE I-FO)
        # closing, or ours before the registry saw it. Periodic reconcile is
        # authoritative for both, so this must not stop the dispatcher.
        return [], [], [DiagnosticLog(
            "warn",
            f"unmatched foc correlation voi={voi} status={foc.kind}",
            voi,
        )]
    if claim.reservation_ref is None:
        return [], [], [DiagnosticLog(
            "error",
            f"uncorrelated legacy claim cannot consume foc voi={voi}",
            voi,
        )]
    if claim.state == RegistryState.RELEASED:
        return [], [], []  # idempotent

    status_upper = foc.kind.upper()

    # EXECUTED is the authoritative fill: foc carries venue_offer_id (fcn does not).
    if "EXECUTED" in status_upper:
        fill = OrderFilled(
            cid=claim.cid,
            venue_offer_id=voi,
            credit_id=None,
            size_usdt=claim.size_usdt,
            fill_rate=foc.rate,
            signal_correlation_id=claim.signal_correlation_id,
            account_id=claim.account_id,
            is_simulated=False,
            venue_seq=foc.venue_seq,
            occurred_at_ms=foc.occurred_at_ms,
            symbol=foc.symbol,
            reservation_ref=claim.reservation_ref,
        )
        return [fill], [RegistryMutation(
            venue_offer_id=voi,
            new_state=RegistryState.RELEASED,
            occurred_at_ms=foc.occurred_at_ms,
        )], []

    # Determine reason
    if "EXPIRED" in status_upper:
        reason = "expired"
    elif "CANCELED" in status_upper or "CANCELLED" in status_upper:
        requested = foc.cancel_requested_at_ms
        if requested is not None and (foc.received_at_ms - requested) <= USER_CANCEL_WINDOW_MS:
            reason = "user_cancel"
        else:
            reason = "venue_cancel"
    else:
        reason = "venue_cancel"  # unknown status, conservative default

    event = ReservationReleased(
        cid=claim.cid,
        venue_offer_id=voi,
        size_usdt=claim.size_usdt,
        reason=reason,
        signal_correlation_id=claim.signal_correlation_id,
        account_id=claim.account_id,
        is_simulated=False,
        venue_seq=foc.venue_seq,
        occurred_at_ms=foc.occurred_at_ms,
        symbol=foc.symbol,
        reservation_ref=claim.reservation_ref,
    )
    mutation = RegistryMutation(
        venue_offer_id=voi,
        new_state=RegistryState.RELEASED,
        occurred_at_ms=foc.occurred_at_ms,
    )
    return [event], [mutation], []


class LegacyVenueHintSink:
    def __init__(
        self, *, registry: Any, bus: Any, persister: EventPersister,
        account_id: str | None,
    ) -> None:
        self._registry = registry
        self._bus = bus
        self._persister = persister
        self._account_id = account_id

    async def offer_closed(self, hint: OfferCloseHint) -> None:
        events, _mutations, diags = _translate_foc(hint, self._registry.snapshot())
        for diag in diags:
            if diag.level == "error":
                raise ReservationCorrelationError(diag.message)
            (log.warning if diag.level == "warn" else log.info)(
                "ws_dispatcher_diag voi=%s msg=%s", diag.venue_offer_id, diag.message,
            )
        for event in events:
            await self.persist_then_publish(event)

    async def credit_closed(self, hint: CreditCloseHint) -> None:
        if self._account_id is None:
            raise ValueError("account_id is required to persist a credit-close event")
        event = CreditClosed(
            symbol=hint.symbol, credit_id=hint.credit_id, amount=hint.amount,
            rate=hint.rate, period_days=hint.period_days, mts_create=hint.mts_create,
            account_id=self._account_id, is_simulated=False,
            venue_seq=hint.venue_seq, occurred_at_ms=hint.occurred_at_ms,
            mts_opening=hint.mts_opening, mts_last_payout=hint.mts_last_payout,
        )
        await self.persist_then_publish(event)

    async def persist_then_publish(self, event: Any) -> None:
        """Keep WS persist failure and durable venue_seq dedup ahead of fanout."""
        try:
            statuses = await self._persister.persist(event)
        except Exception as exc:
            log.critical(
                "ws_dispatcher_persist_failed err=%r event=%s — SoT write lost, skipping publish",
                exc, type(event).__name__,
            )
            return
        if statuses and not statuses[0]:
            log.debug(
                "ws_event_deduped_skip_publish event=%s venue_seq=%s",
                type(event).__name__, getattr(event, "venue_seq", None),
            )
            return
        try:
            await self._bus.publish(event)
        except Exception as exc:
            log.critical("ws_dispatcher_publish_failed err=%r event=%s", exc, type(event).__name__)

    async def offer_gone(self, venue_offer_id: str, *, occurred_at_ms: int) -> bool:
        claim = self._registry.snapshot().get(venue_offer_id)
        if claim is None:
            log.warning(
                "fill_tracker_venue_gone_unknown voi=%s"
                " — not in registry (boot-before-claim or already cleaned)", venue_offer_id,
            )
            return True
        if claim.state == RegistryState.RELEASED:
            log.debug("fill_tracker_dedup voi=%s — registry RELEASED", venue_offer_id)
            return True
        if claim.reservation_ref is None:
            raise InvariantError(
                f"fill_tracker cannot release uncorrelated legacy claim voi={venue_offer_id}"
            )
        if self._account_id is None:
            raise ValueError("account_id is required to persist an offer-gone event")
        release = ReservationReleased(
            cid=claim.cid, venue_offer_id=venue_offer_id, size_usdt=claim.size_usdt,
            reason="missing_from_venue", signal_correlation_id=claim.signal_correlation_id,
            account_id=self._account_id, is_simulated=False, occurred_at_ms=occurred_at_ms,
            symbol=claim.symbol, reservation_ref=claim.reservation_ref,
        )
        try:
            await self._persister.persist(release)
        except Exception as exc:
            log.critical(
                "fill_tracker_persist_failed err=%r voi=%s — SoT write lost; "
                "retrying next poll (boot recovery is backstop)", exc, venue_offer_id,
            )
            return False
        await self._bus.publish(release)
        return True
