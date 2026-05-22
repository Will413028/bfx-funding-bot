"""RestPollingFillTracker — atomic /offers + /credits poll, venue_offer_id diff to events.

Scaffolded in 4.2; gated off by default (BFX_FILL_TRACKER_ENABLED=false).
4.4 ships BitfinexLiveExecutor with WebSocket-primary fill path; this REST
polling tracker becomes the reconciliation backup.

Design notes (4.2 vs 4.4):
- 4.2: REST polling only. Bridge between /offers and /credits is NOT cid
  (Bitfinex FundingCredit response loses the originating offer's cid).
  We track venue_offer_id (str(o[0]) from FundingOffer) as the state key.
  Diff = offer present last tick + absent this tick → emit status_change
  status=filled_or_cancelled (coarse — 4.4 refines with WS fcn/fcu events
  that carry FUNDING_OFFER_ID linkage to credits).
- /credits is fetched but only used to validate atomicity (both endpoints
  succeed) + schema-validated. No diff bridge in 4.2.

Atomic poll (CC3): both GET calls must succeed or the entire tick aborts.
last_state preserved; next tick retries fresh.

CC4 paper_ prefix invariant: defense-in-depth. registry.build_executor
(Task 17) catches the paper-executor + fill-tracker combo at startup. This
runtime check catches bypass paths — if a paper_ prefixed venue_offer_id
ever appears in last_state and we try to emit a status_change for it,
something is fundamentally wrong → raise InvariantError → daemon
TaskGroup cancel → Koyeb restart.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

import httpx

from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import ReservationReleased
from bfx_funding_bot.modules.execution.registry_offers import (
    OfferRegistry,
    RegistryState,
)
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.schemas import (
    EventType,
    HealthStatus,
    HealthTarget,
    Level,
    Phase,
    StrategyName,
)

log = logging.getLogger(__name__)

CONSECUTIVE_FAIL_THRESHOLD = 3
_OFFERS_ENDPOINT = "/v2/auth/r/funding/offers"
_CREDITS_ENDPOINT = "/v2/auth/r/funding/credits"


class _AxiomProtocol(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


class InvariantError(RuntimeError):
    """fill_tracker observed a paper_ prefixed venue_offer_id — env config
    mismatch (CC4 defense-in-depth; registry should have caught this at startup)."""


class RestPollingFillTracker:
    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        axiom: _AxiomProtocol,
        probe: HealthProbe,
        bus: DomainEventBus,
        phase: Phase,
        strategy: StrategyName,
        cell: str,
        account_id: str,
        registry: OfferRegistry,
        poll_interval_s: float = 30.0,
    ) -> None:
        self.http = http
        self.axiom = axiom
        self.probe = probe
        self._bus = bus
        self.phase = phase
        self.strategy = strategy
        self.cell = cell
        self.account_id = account_id
        self._registry = registry
        self.poll_interval_s = poll_interval_s
        # venue_offer_id (str) → {cid: int, status: str, size: float}
        self._last_state: dict[str, dict[str, Any]] = {}
        self._consecutive_failures = 0

    async def poll_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self._tick()
                self.probe.record_heartbeat("fill_tracker")
            except InvariantError:
                raise  # propagate to TaskGroup
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.poll_interval_s)
            except TimeoutError:
                continue

    async def _tick(self) -> None:
        current: dict[str, dict[str, Any]] = {}
        try:
            # return_exceptions=True matches healthz.py sibling pattern — if one GET
            # raises, the other still drains cleanly instead of being cancelled mid-flight.
            offers_resp, credits_resp = await asyncio.gather(
                self.http.get(_OFFERS_ENDPOINT),
                self.http.get(_CREDITS_ENDPOINT),
                return_exceptions=True,
            )
            if isinstance(offers_resp, BaseException):
                raise RuntimeError(f"offers_get_failed: {offers_resp!r}") from offers_resp
            if isinstance(credits_resp, BaseException):
                raise RuntimeError(f"credits_get_failed: {credits_resp!r}") from credits_resp
            if offers_resp.status_code != 200 or credits_resp.status_code != 200:
                raise RuntimeError(
                    f"non_200 offers={offers_resp.status_code} "
                    f"credits={credits_resp.status_code}"
                )
            offers = offers_resp.json()
            _credits = credits_resp.json()  # fetched for atomicity; not used as diff bridge in 4.2
            # Build current state inside the try — malformed venue responses
            # (non-list, rows shorter than 21 elements) must fail the tick cleanly,
            # not kill poll_loop with an IndexError / TypeError.
            if not isinstance(offers, list):
                raise RuntimeError(f"offers_not_list: type={type(offers).__name__}")
            for o in offers:
                if not isinstance(o, list) or len(o) < 21:
                    raise RuntimeError(f"offer_row_malformed: {o!r}")
                venue_id = str(o[0])
                cid = o[20]
                if cid is None:
                    continue
                current[venue_id] = {"cid": cid, "status": "ACTIVE", "size": abs(float(o[5]))}
        except InvariantError:
            raise
        except Exception as exc:
            self._consecutive_failures += 1
            log.warning("fill_tracker_tick_aborted attempt=%d err=%r",
                        self._consecutive_failures, exc)
            if self._consecutive_failures >= CONSECUTIVE_FAIL_THRESHOLD:
                await self._emit_degraded(str(exc))
            return  # last_state preserved; next tick retries

        self._consecutive_failures = 0

        await self._diff_and_emit(current)
        self._last_state = current

    async def _diff_and_emit(self, current: dict[str, dict[str, Any]]) -> None:
        """Disappearance = present in last_state, absent from current → reservation released.

        Registry-aware dedup (4.4a):
        - If voi not in registry: boot-before-claim race — skip silently.
        - If registry state == RELEASED: WS already handled it — skip (dedup).
        - If registry state == CLAIMED: emit with registry's correlation_id (G3 fix).
        """
        snapshot = self._registry.snapshot()
        for venue_offer_id, _prev in self._last_state.items():
            if venue_offer_id in current:
                continue
            # CC4 defense-in-depth: paper_ should never reach here.
            if venue_offer_id.startswith("paper_"):
                raise InvariantError(
                    f"fill_tracker last_state contained paper venue_offer_id="
                    f"{venue_offer_id} — registry CC4 should have prevented this; "
                    "check BFX_EXECUTOR / BFX_FILL_TRACKER_ENABLED wiring."
                )
            claim = snapshot.get(venue_offer_id)
            if claim is None:
                log.warning(
                    "fill_tracker_venue_gone_unknown voi=%s"
                    " — not in registry (boot-before-claim or already cleaned)",
                    venue_offer_id,
                )
                continue
            if claim.state == RegistryState.RELEASED:
                log.debug(
                    "fill_tracker_dedup voi=%s — registry RELEASED",
                    venue_offer_id,
                )
                continue
            # CLAIMED: emit with registry-sourced fields (G3 deterministic correlation_id)
            await self._bus.publish(ReservationReleased(
                cid=claim.cid,
                venue_offer_id=venue_offer_id,
                size_usdt=claim.size_usdt,
                reason="missing_from_venue",
                signal_correlation_id=claim.signal_correlation_id,
                account_id=self.account_id,
                is_simulated=False,
                occurred_at_ms=int(time.time() * 1000),
            ))

    async def _emit_degraded(self, reason: str) -> None:
        self.probe.update(
            HealthTarget.FILL_TRACKER, HealthStatus.DEGRADED,
            error_message=reason,
        )
        await self.axiom.emit({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": Level.WARN.value,
            "phase": self.phase.value,
            "strategy": None, "cell": None,
            "event_type": EventType.HEALTH_CHECK.value,
            "correlation_id": str(uuid4()),
            "account_id": self.account_id,
            "payload": {
                "check_target": HealthTarget.FILL_TRACKER.value,
                "status": HealthStatus.DEGRADED.value,
                "error_message": (
                    f"{self._consecutive_failures} consecutive ticks failed: {reason}"
                ),
            },
        })
