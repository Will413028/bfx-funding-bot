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

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

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


log = logging.getLogger(__name__)


class _AxiomProtocol(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


class _WSClientProtocol(Protocol):
    def events(self) -> AsyncIterator[BfxWSEvent]: ...


class BitfinexLiveWSDispatcher:
    """Consume BitfinexAuthWSClient stream → query OfferRegistry + recent_cancels
    → translate_bfx_event → publish domain events to bus.

    Per spec §6.2 / §4 (G4): bounded queue + queue-depth observability.
    OOO staging buffer for fcn-before-claim race (200ms TTL).
    """

    OOO_STAGING_TTL_MS = 200
    RECENT_CANCELS_TTL_MS = 60_000

    def __init__(
        self,
        *,
        ws_client: _WSClientProtocol,
        registry: Any,  # OfferRegistry
        bus: Any,       # DomainEventBus
        axiom: _AxiomProtocol,
        clock: Callable[[], int] | None = None,
        queue_max: int = 10_000,
    ) -> None:
        self._ws_client = ws_client
        self._registry = registry
        self._bus = bus
        self._axiom = axiom
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._queue: asyncio.Queue[BfxWSEvent] = asyncio.Queue(maxsize=queue_max)
        self._queue_max = queue_max
        self._staging_buffer: dict[str, tuple[BfxWSEvent, int]] = {}
        self._recent_cancels: dict[str, int] = {}
        self._last_depth_emit_ms: int = 0
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def handle_cancel_requested(self, event: Any) -> None:
        """Bus subscriber for CancelRequested — tracks recent cancels for 60s."""
        self._recent_cancels[event.venue_offer_id] = event.requested_at_ms

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main loop: drain WS events + drain staging buffer."""
        producer = asyncio.create_task(self._produce(stop_event))
        try:
            while not stop_event.is_set():
                try:
                    bfx_event = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                except TimeoutError:
                    self._tick_maintenance()
                    continue
                await self._process(bfx_event)
                self._tick_maintenance()
        finally:
            producer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await producer

    async def _produce(self, stop_event: asyncio.Event) -> None:
        async for ev in self._ws_client.events():
            if stop_event.is_set():
                return
            if self._queue.qsize() >= int(self._queue_max * 0.8):
                log.warning("ws_dispatcher_queue_high depth=%d max=%d",
                            self._queue.qsize(), self._queue_max)
            await self._queue.put(ev)

    async def _process(self, bfx_event: BfxWSEvent) -> None:
        now_ms = self._clock()
        snapshot = self._registry.snapshot()
        events, _mutations, diags = translate_bfx_event(
            bfx_event, snapshot, dict(self._recent_cancels), now_ms,
        )
        for d in diags:
            (log.warning if d.level == "warn" else log.info)(
                "ws_dispatcher_diag voi=%s msg=%s",
                d.venue_offer_id, d.message,
            )
            # If diag is "fcn before claimed" (stage hint) — stage in OOO buffer
            if d.venue_offer_id and "stage" in d.message.lower():
                self._staging_buffer[d.venue_offer_id] = (
                    bfx_event, now_ms + self.OOO_STAGING_TTL_MS,
                )
        for ev in events:
            try:
                await self._bus.publish(ev)
            except Exception as e:
                log.critical("ws_dispatcher_publish_failed err=%r event=%s",
                             e, type(ev).__name__)

    def _tick_maintenance(self) -> None:
        now_ms = self._clock()
        # Cleanup expired staging buffer entries
        expired = [voi for voi, (_, exp) in self._staging_buffer.items() if exp < now_ms]
        for voi in expired:
            log.error("ws_dispatcher_ooo_drop voi=%s — fcn TTL expired, _realized may be wrong",
                      voi)
            del self._staging_buffer[voi]
        # Drain staging buffer: re-process if claim now exists
        snapshot = self._registry.snapshot()
        ready = [voi for voi in list(self._staging_buffer.keys()) if voi in snapshot]
        for voi in ready:
            bfx_event, _ = self._staging_buffer.pop(voi)
            t = asyncio.create_task(self._process(bfx_event))
            self._background_tasks.add(t)
            t.add_done_callback(self._background_tasks.discard)

        # Cleanup expired recent_cancels
        cancel_cutoff = now_ms - self.RECENT_CANCELS_TTL_MS
        self._recent_cancels = {
            voi: ts for voi, ts in self._recent_cancels.items() if ts >= cancel_cutoff
        }

        # Emit queue depth health every 30s
        if now_ms - self._last_depth_emit_ms > 30_000:
            self._last_depth_emit_ms = now_ms
            depth = self._queue.qsize()
            log.info("ws_dispatcher_queue_depth depth=%d max=%d", depth, self._queue_max)
