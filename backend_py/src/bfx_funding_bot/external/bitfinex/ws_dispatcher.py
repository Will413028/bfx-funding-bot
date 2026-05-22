"""BitfinexLiveWSDispatcher — Bitfinex WS event → DomainEventBus (Phase 4.4a).

Anti-corruption layer. Pure translate_bfx_event(bfx_ev, snapshot, cancels, now_ms)
+ I/O shell (Task 16).

Per spec §6.2 dispatch table:
  FcnEvent + CLAIMED → OrderFilled + state RELEASED
  FcnEvent + not_in_registry → diag (dispatcher stages OOO)
  FocEvent EXECUTED + CLAIMED → no-op (fcn handled it)
  FocEvent CANCELED + recent_cancel ≤5s → user_cancel
  FocEvent CANCELED otherwise → venue_cancel
  FocEvent EXPIRED → expired
  any event on RELEASED → idempotent no-op
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bfx_funding_bot.external.bitfinex.auth_ws import (
    BfxWSEvent,
    FcnEvent,
    FcuEvent,
    FocEvent,
)
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.registry_offers import (
    ClaimRecord,
    DiagnosticLog,
    RegistryState,
)

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
) -> tuple[list[Any], list[RegistryMutation], list[DiagnosticLog]]:
    """Pure mapping. Returns (domain_events, mutations, diagnostics)."""
    if isinstance(bfx_event, FcnEvent):
        return _translate_fcn(bfx_event, snapshot, now_ms)
    if isinstance(bfx_event, FocEvent):
        return _translate_foc(bfx_event, snapshot, recent_cancels, now_ms)
    if isinstance(bfx_event, FcuEvent):
        return [], [], []  # 4.4a: rate updates not modeled
    return [], [], []  # Heartbeat / AuthAck / ChannelInfo / Unknown


def _translate_fcn(
    fcn: FcnEvent,
    snapshot: dict[str, ClaimRecord],
    now_ms: int,
) -> tuple[list[Any], list[RegistryMutation], list[DiagnosticLog]]:
    voi = str(fcn.offer_id_meta) if fcn.offer_id_meta is not None else None
    if voi is None:
        return [], [], [DiagnosticLog(
            "warn",
            f"fcn credit_id={fcn.credit_id} missing offer_id_meta — cannot map back",
        )]

    claim = snapshot.get(voi)
    if claim is None:
        return [], [], [DiagnosticLog(
            "info",
            f"fcn for voi={voi} not in registry — stage in OOO buffer",
            voi,
        )]
    if claim.state == RegistryState.RELEASED:
        return [], [], []  # idempotent

    event = OrderFilled(
        cid=claim.cid,
        venue_offer_id=voi,
        credit_id=str(fcn.credit_id),
        size_usdt=claim.size_usdt,
        fill_rate=fcn.rate,
        signal_correlation_id=claim.signal_correlation_id,
        account_id=claim.account_id,
        is_simulated=False,
        venue_seq=fcn.raw_seq,
        occurred_at_ms=fcn.mts_create,
    )
    mutation = RegistryMutation(
        venue_offer_id=voi,
        new_state=RegistryState.RELEASED,
        occurred_at_ms=fcn.mts_create,
    )
    return [event], [mutation], []


def _translate_foc(
    foc: FocEvent,
    snapshot: dict[str, ClaimRecord],
    recent_cancels: dict[str, int],
    now_ms: int,
) -> tuple[list[Any], list[RegistryMutation], list[DiagnosticLog]]:
    voi = foc.venue_offer_id
    claim = snapshot.get(voi)

    if claim is None:
        return [], [], [DiagnosticLog(
            "info",
            f"foc voi={voi} status={foc.status} not in registry",
            voi,
        )]
    if claim.state == RegistryState.RELEASED:
        return [], [], []  # idempotent

    status_upper = foc.status.upper()

    # EXECUTED handled by fcn — foc EXECUTED is redundant
    if "EXECUTED" in status_upper:
        return [], [], [DiagnosticLog(
            "info",
            f"foc EXECUTED voi={voi} — redundant (fcn should have handled)",
            voi,
        )]

    # Determine reason
    if "EXPIRED" in status_upper:
        reason = "expired"
    elif "CANCELED" in status_upper or "CANCELLED" in status_upper:
        requested = recent_cancels.get(voi)
        if requested is not None and (now_ms - requested) <= USER_CANCEL_WINDOW_MS:
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
        venue_seq=foc.raw_seq,
        occurred_at_ms=foc.mts_update,
    )
    mutation = RegistryMutation(
        venue_offer_id=voi,
        new_state=RegistryState.RELEASED,
        occurred_at_ms=foc.mts_update,
    )
    return [event], [mutation], []
