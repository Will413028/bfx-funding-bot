"""Admin smoke-test runner — synthesize paper offer through prod chain.

Boot-time auto + admin endpoint share SmokeRunner core (this file). Synthetic
events tagged account_id="smoke_test"; prod ledger filters them out via
account_id guard (modules/execution/ledger.py).

Design: docs/superpowers/specs/2026-05-22-admin-smoke-test-design.md
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import uuid4

from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    ExecutorPort,
    SubmittedOrder,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
    Phase,
    StrategyName,
)

log = logging.getLogger(__name__)

# Module-level single-flight lock. Both boot smoke and endpoint share it.
# Router pre-checks .locked() to return 409; SmokeRunner uses async-with to
# serialize (no contention expected because router pre-check filters).
_SMOKE_LOCK = asyncio.Lock()


SMOKE_ACCOUNT_ID = "smoke_test"
SMOKE_SIZE_USDT = 1.0
SMOKE_RATE = 0.0001
SMOKE_DURATION_DAYS = 2


class _AxiomProtocol(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


class _AxiomQueryProtocol(Protocol):
    async def query_order_events(
        self, account_id: str, since: datetime,
    ) -> list[dict[str, Any]]: ...


class SmokeAssertionError(Exception):
    """Raised when smoke chain produces unexpected state."""


@dataclass(frozen=True)
class SmokeResult:
    status: Literal["pass", "fail"]
    level: Literal["L2", "L3"]
    checks: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    error: str | None = None


@dataclass
class EventRecorder:
    """Spy that captures bus events matching a given account_id."""
    account_id: str
    events: list[Any] = field(default_factory=list)

    async def record(self, event: Any) -> None:
        if getattr(event, "account_id", None) == self.account_id:
            self.events.append(event)


class SmokeRunner:
    def __init__(
        self,
        *,
        executor: ExecutorPort,
        bus: DomainEventBus,
        axiom_client: _AxiomProtocol,
        axiom_query: _AxiomQueryProtocol,
        phase: Phase,
        strategy: StrategyName,
        cell: str,
    ) -> None:
        self._executor = executor
        self._bus = bus
        self._axiom = axiom_client
        self._axiom_query = axiom_query
        self._phase = phase
        self._strategy = strategy
        self._cell = cell

    async def aclose(self) -> None:
        """Clean up internal axiom query adapter & handles.

        Replaces daemon `_run()` direct access to `self._axiom_query` (which
        required `# type: ignore[attr-defined]` — adapter shape not on
        SmokeRunner public surface). Phase 4.4 prework Followup (a).
        """
        if self._axiom_query is not None and hasattr(self._axiom_query, "aclose"):
            await self._axiom_query.aclose()

    async def run_l2(self) -> SmokeResult:
        async with _SMOKE_LOCK:
            return await self._run_l2_unlocked()

    async def _run_l2_unlocked(self) -> SmokeResult:
        start = time.monotonic()
        checks: dict[str, Any] = {}
        recorder = EventRecorder(account_id=SMOKE_ACCOUNT_ID)
        try:
            _before_ts, result = await self._run_chain(recorder)
            self._assert_l2(result, recorder, checks)
            duration_ms = int((time.monotonic() - start) * 1000)
            log.info("smoke_l2_passed duration_ms=%d", duration_ms)
            return SmokeResult(
                status="pass", level="L2", checks=checks, duration_ms=duration_ms,
            )
        except SmokeAssertionError as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            log.warning("smoke_l2_failed reason=%r duration_ms=%d", exc, duration_ms)
            return SmokeResult(
                status="fail", level="L2", checks=checks,
                duration_ms=duration_ms, error=str(exc),
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            log.exception("smoke_l2_unexpected_error")
            return SmokeResult(
                status="fail", level="L2", checks=checks,
                duration_ms=duration_ms, error=f"unexpected: {exc!r}",
            )

    async def _run_chain(
        self, recorder: EventRecorder,
    ) -> tuple[datetime, SubmittedOrder]:
        """Execute the wrapped executor chain with smoke account_id.

        Returns (before_timestamp, submit_result). Bus subscriptions are
        scoped via async-with — handlers auto-unsubscribe on exit.
        """
        decision = DecisionPayload(
            decision_outcome=DecisionOutcome.POST,
            signal_correlation_id=uuid4(),
            offer_rate=SMOKE_RATE,
            offer_amount_usdt=SMOKE_SIZE_USDT,
            offer_duration_days=SMOKE_DURATION_DAYS,
        )
        smoke_ctx = AccountContext(
            account_id=SMOKE_ACCOUNT_ID,
            credentials=Credentials(api_key="smoke", api_secret="smoke"),
            allocation_cap_usdt=Decimal("1000"),
        )
        before_ts = datetime.now(UTC)
        async with (
            self._bus.subscription(ReservationClaimed, recorder.record),
            self._bus.subscription(OrderFilled, recorder.record),
            self._bus.subscription(ReservationReleased, recorder.record),
        ):
            result = await self._executor.submit(decision, smoke_ctx)
        return before_ts, result

    def _assert_l2(
        self,
        result: SubmittedOrder,
        recorder: EventRecorder,
        checks: dict[str, Any],
    ) -> None:
        checks["executor_returned_filled"] = result.status == "filled"
        checks["events_count"] = len(recorder.events)
        if not checks["executor_returned_filled"]:
            raise SmokeAssertionError(
                f"executor returned status={result.status!r}, expected 'filled'",
            )
        if len(recorder.events) != 2:
            raise SmokeAssertionError(
                f"expected 2 events, got {len(recorder.events)}",
            )
        e0, e1 = recorder.events
        if not isinstance(e0, ReservationClaimed):
            raise SmokeAssertionError(
                f"event[0] type={type(e0).__name__}, expected ReservationClaimed",
            )
        if not isinstance(e1, OrderFilled):
            raise SmokeAssertionError(
                f"event[1] type={type(e1).__name__}, expected OrderFilled",
            )
        expected_size = Decimal(str(SMOKE_SIZE_USDT))
        if e0.size_usdt != expected_size:
            raise SmokeAssertionError(
                f"event[0].size_usdt={e0.size_usdt}, expected {expected_size}",
            )
        if e1.size_usdt != expected_size:
            raise SmokeAssertionError(
                f"event[1].size_usdt={e1.size_usdt}, expected {expected_size}",
            )
        if not e0.is_simulated or not e1.is_simulated:
            raise SmokeAssertionError("expected is_simulated=True on both events")
        if e0.account_id != SMOKE_ACCOUNT_ID or e1.account_id != SMOKE_ACCOUNT_ID:
            raise SmokeAssertionError(
                f"events tagged with wrong account_id "
                f"(got {e0.account_id!r}/{e1.account_id!r}, expected {SMOKE_ACCOUNT_ID!r})",
            )
        checks["l2_passed"] = True

    async def run_l3(self) -> SmokeResult:
        async with _SMOKE_LOCK:
            return await self._run_l3_unlocked()

    async def _run_l3_unlocked(
        self,
        *,
        poll_attempts: int = 5,
        poll_interval_s: float = 3.0,
    ) -> SmokeResult:
        """L2 + Axiom round-trip verification.

        After L2 passes (chain published to bus + axiom_sink emitted to Axiom),
        poll Axiom APL up to `poll_attempts x poll_interval_s` seconds for the
        events to appear. Tests override poll parameters for speed.
        """
        start = time.monotonic()
        checks: dict[str, Any] = {}
        recorder = EventRecorder(account_id=SMOKE_ACCOUNT_ID)
        # ── L2 phase ──
        try:
            before_ts, result = await self._run_chain(recorder)
            self._assert_l2(result, recorder, checks)
        except SmokeAssertionError as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            log.warning("smoke_l3_l2_failed reason=%r", exc)
            return SmokeResult(
                status="fail", level="L2", checks=checks,
                duration_ms=duration_ms, error=str(exc),
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            log.exception("smoke_l3_l2_unexpected_error")
            return SmokeResult(
                status="fail", level="L2", checks=checks,
                duration_ms=duration_ms, error=f"unexpected: {exc!r}",
            )

        # ── L3 phase (round-trip Axiom query poll) ──
        try:
            await self._poll_axiom_round_trip(
                before_ts, checks,
                poll_attempts=poll_attempts, poll_interval_s=poll_interval_s,
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            log.info("smoke_l3_passed duration_ms=%d", duration_ms)
            return SmokeResult(
                status="pass", level="L3", checks=checks, duration_ms=duration_ms,
            )
        except SmokeAssertionError as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            log.warning("smoke_l3_failed reason=%r", exc)
            return SmokeResult(
                status="fail", level="L3", checks=checks,
                duration_ms=duration_ms, error=str(exc),
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            log.exception("smoke_l3_unexpected_error")
            return SmokeResult(
                status="fail", level="L3", checks=checks,
                duration_ms=duration_ms, error=f"unexpected: {exc!r}",
            )

    async def _poll_axiom_round_trip(
        self,
        since: datetime,
        checks: dict[str, Any],
        *,
        poll_attempts: int,
        poll_interval_s: float,
    ) -> None:
        from bfx_funding_bot.modules.marketfeed.schemas import EventType

        required_types = {
            EventType.RESERVATION_CLAIMED.value,
            EventType.ORDER_FILL.value,
        }
        last_seen = 0
        last_types: set[str] = set()
        for attempt in range(1, poll_attempts + 1):
            events = await self._axiom_query.query_order_events(
                SMOKE_ACCOUNT_ID, since,
            )
            last_seen = len(events)
            last_types = {
                et for e in events
                if isinstance(et := e.get("event_type"), str)
            }
            if last_seen >= 2 and required_types.issubset(last_types):
                checks["axiom_events_seen"] = last_seen
                checks["axiom_event_types"] = sorted(last_types)
                return
            if attempt < poll_attempts:
                await asyncio.sleep(poll_interval_s)
        # Exhausted attempts
        checks["axiom_events_seen"] = last_seen
        checks["axiom_event_types"] = sorted(last_types)
        raise SmokeAssertionError(
            f"axiom round-trip timeout: seen={last_seen} types={sorted(last_types)} "
            f"required={sorted(required_types)}",
        )
