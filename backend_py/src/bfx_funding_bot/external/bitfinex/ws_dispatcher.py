"""BitfinexLiveWSDispatcher — Bitfinex WS event → DomainEventBus (Phase 4.4a).

Anti-corruption layer. Pure translate_bfx_event(bfx_ev, snapshot, cancels, now_ms)
+ I/O shell (Task 16).

Per spec §6.2 dispatch table:
  FocEvent EXECUTED + CLAIMED → OrderFilled + state RELEASED (authoritative fill)
  FocEvent CANCELED + recent_cancel ≤5s → user_cancel
  FocEvent CANCELED otherwise → venue_cancel
  FocEvent EXPIRED → expired
  FcnEvent → informational no-op (credit events carry no offer id)
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
    FccEvent,
    FcnEvent,
    FcuEvent,
    FocEvent,
)
from bfx_funding_bot.modules.execution.event_store.persister import (
    EventPersister,
    NoopEventPersister,
)
from bfx_funding_bot.modules.execution.events import (
    CreditClosed,
    OrderFilled,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.registry_offers import (
    ClaimRecord,
    DiagnosticLog,
    RegistryState,
    ReservationCorrelationError,
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
    *,
    account_id: str = "default",
) -> tuple[list[Any], list[RegistryMutation], list[DiagnosticLog]]:
    """Pure mapping. Returns (domain_events, mutations, diagnostics)."""
    if isinstance(bfx_event, FcnEvent):
        return [], [], []  # informational only — no offer id; foc EXECUTED is the fill signal
    if isinstance(bfx_event, FocEvent):
        return _translate_foc(bfx_event, snapshot, recent_cancels, now_ms)
    if isinstance(bfx_event, FccEvent):
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
            occurred_at_ms=bfx_event.mts_update,
        )
        return [closed], [], []
    if isinstance(bfx_event, FcuEvent):
        return [], [], []  # 4.4a: rate updates not modeled
    return [], [], []  # Heartbeat / AuthAck / ChannelInfo / Unknown


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
            "error",
            f"unmatched foc correlation voi={voi} status={foc.status}",
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

    status_upper = foc.status.upper()

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
            venue_seq=foc.raw_seq,
            occurred_at_ms=foc.mts_update,
            symbol=foc.symbol,
            reservation_ref=claim.reservation_ref,
        )
        return [fill], [RegistryMutation(
            venue_offer_id=voi,
            new_state=RegistryState.RELEASED,
            occurred_at_ms=foc.mts_update,
        )], []

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
        symbol=foc.symbol,
        reservation_ref=claim.reservation_ref,
    )
    mutation = RegistryMutation(
        venue_offer_id=voi,
        new_state=RegistryState.RELEASED,
        occurred_at_ms=foc.mts_update,
    )
    return [event], [mutation], []


log = logging.getLogger(__name__)


class _EventSink(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


class _WSClientProtocol(Protocol):
    def events(self) -> AsyncIterator[BfxWSEvent]: ...


class BitfinexLiveWSDispatcher:
    """Consume BitfinexAuthWSClient stream → query OfferRegistry + recent_cancels
    → translate_bfx_event → publish domain events to bus.

    Per spec §6.2 / §4 (G4): bounded queue + queue-depth observability.
    """

    RECENT_CANCELS_TTL_MS = 60_000

    def __init__(
        self,
        *,
        ws_client: _WSClientProtocol,
        registry: Any,  # OfferRegistry
        bus: Any,       # DomainEventBus
        event_sink: _EventSink,
        clock: Callable[[], int] | None = None,
        queue_max: int = 10_000,
        persister: EventPersister | None = None,
        account_id: str = "default",
    ) -> None:
        self._ws_client = ws_client
        self._registry = registry
        self._bus = bus
        self._events = event_sink
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._queue: asyncio.Queue[BfxWSEvent] = asyncio.Queue(maxsize=queue_max)
        self._queue_max = queue_max
        self._recent_cancels: dict[str, int] = {}
        self._last_depth_emit_ms: int = 0
        self._persister: EventPersister = persister or NoopEventPersister()
        self._account_id = account_id

    @property
    def queue_depth(self) -> int:
        """Read-only observability accessor (Prometheus saturation gauge
        bfx_ws_dispatcher_queue_depth reads this at scrape time). No behavior."""
        return self._queue.qsize()

    @property
    def queue_capacity(self) -> int:
        """Read-only observability accessor — the bounded queue's maxsize."""
        return self._queue_max

    async def handle_cancel_requested(self, event: Any) -> None:
        """Bus subscriber for CancelRequested — tracks recent cancels for 60s."""
        self._recent_cancels[event.venue_offer_id] = event.requested_at_ms

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main loop: drain WS events; periodic maintenance on idle ticks."""
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
            account_id=self._account_id,
        )
        for d in diags:
            if d.level == "error":
                raise ReservationCorrelationError(d.message)
            (log.warning if d.level == "warn" else log.info)(
                "ws_dispatcher_diag voi=%s msg=%s",
                d.venue_offer_id, d.message,
            )
        for ev in events:
            await self._persist_then_publish(ev)

    async def _persist_then_publish(self, ev: Any) -> None:
        """Durable SoT write before in-memory fanout. WS events carry venue_seq
        so re-delivery is deduped by the store; on persist failure we skip
        publish to keep in-memory projections from drifting ahead of PG.

        Dedup path: if store.append() returned False (event already in PG),
        persist() propagates [False] here → we skip bus.publish so in-memory
        projections never see a phantom event that has no new PG row backing it.
        """
        try:
            statuses = await self._persister.persist(ev)
        except Exception as e:
            log.critical(
                "ws_dispatcher_persist_failed err=%r event=%s — SoT write lost, skipping publish",
                e, type(ev).__name__,
            )
            return
        # statuses is a 1-element list (we persist one event per call here).
        # False means the store silently deduped (same venue_seq already in PG).
        if statuses and not statuses[0]:
            log.debug(
                "ws_event_deduped_skip_publish event=%s venue_seq=%s",
                type(ev).__name__,
                getattr(ev, "venue_seq", None),
            )
            return
        try:
            await self._bus.publish(ev)
        except Exception as e:
            log.critical("ws_dispatcher_publish_failed err=%r event=%s", e, type(ev).__name__)

    def _tick_maintenance(self) -> None:
        now_ms = self._clock()
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
