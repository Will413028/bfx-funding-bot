"""Operator-request scenarios over the legacy and the ledger stacks.

One UNKNOWN submit, the observations that follow it, and the port a request goes
through; each driver writes through its own authority's paths (legacy: the event
store and its projections; ledger: the journal and the observation cycle), at times
that keep the attempt inside its settle window so only an operator resolves it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select

from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.events import (
    ReservationIntent,
    ReservationUnknown,
    VenueOfferObservation,
)
from bfx_funding_bot.modules.execution.legacy_operator_reads import LegacyOperatorReads
from bfx_funding_bot.modules.execution.submit_outcomes import SubmissionAttemptPayload
from bfx_funding_bot.modules.execution.uncertainty_resolution import (
    LegacyOperatorResolution,
    ResolutionScope,
    UncertaintyResolutionRequests,
    UncertaintyResolutionWorker,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    UncertaintyResolutionRequestRow,
)
from bfx_funding_bot.modules.ledger import (
    OperatorReads,
    OperatorResolution,
    ResolutionIntent,
    observation_evidence_ref,
)
from bfx_funding_bot.modules.ledger.wiring import build_operator_reads, build_operator_resolution

from ..test_ledger_unknown_resolver_pg import (
    Clock,
    FakeVenue,
    run_cycle,
    seed_unknown,
    start,
    venue_offer,
)
from .stacks import ACCOUNT, ENVIRONMENT, SCOPE, Stack

OPERATOR = "operator-1"
RSCOPE = ResolutionScope(ACCOUNT, ENVIRONMENT)
WORKER_NOW = 3_000_000


class Allow:
    async def __call__(self, session, *, account_id, user) -> bool:
        return True


class Driver:
    """What a contract test needs of one authority's operator request path."""

    name: str
    resolution: OperatorResolution
    reads: OperatorReads

    def __init__(self, stack: Stack) -> None:
        self.stack = stack
        self.factory: Any = stack.factory

    async def open_unknown(self) -> UUID:
        raise NotImplementedError

    async def observe(self, offers: tuple[str, ...] = ()) -> str:
        """A newer, complete observation naming ``offers`` (matching the UNKNOWN); its ref."""
        raise NotImplementedError

    def other_authority_ref(self) -> str:
        """A well-formed reference of the other authority."""
        raise NotImplementedError

    def another_ref(self) -> str:
        """A well-formed reference of this authority that is not the one observed."""
        raise NotImplementedError

    async def event_log_rows(self) -> int:
        async with self.factory() as session:
            return int(await session.scalar(select(func.count()).select_from(EventLogRow)) or 0)

    def intent(
        self, uncertainty: UUID, ref: str, action: str = "mark_not_accepted", **changes: Any
    ) -> ResolutionIntent:
        return ResolutionIntent(
            uncertainty, action, ref, OPERATOR, **changes  # type: ignore[arg-type]
        )

    async def request(self, item: ResolutionIntent) -> UncertaintyResolutionRequestRow:
        async with self.factory.begin() as session:
            row = await UncertaintyResolutionRequests(RSCOPE, self.resolution).request(
                session, item, now_ms=2_500_000
            )
            return row

    async def settle(self, request_id: UUID) -> UncertaintyResolutionRequestRow:
        worker = UncertaintyResolutionWorker(
            session_factory=self.factory, scope=RSCOPE, authority=Allow(),
            clock=lambda: WORKER_NOW, resolution=self.resolution,
        )
        await worker.process(request_id)
        return await self.row(request_id)

    async def row(self, request_id: UUID) -> UncertaintyResolutionRequestRow:
        async with self.factory() as session:
            return await UncertaintyResolutionRequests(RSCOPE).get(session, request_id)

    async def view(self, uncertainty: UUID) -> Any:
        async with self.factory() as session:
            return await self.reads.get_uncertainty(session, SCOPE, uncertainty)


class LedgerDriver(Driver):
    name = "ledger"

    def __init__(self, stack: Stack) -> None:
        super().__init__(stack)
        self.resolution = build_operator_resolution()
        self.reads = build_operator_reads()
        self.book = stack.builders.book
        self.at = 1_100_000  # after the UNKNOWN (1_050_000), inside the settle window

    async def open_unknown(self) -> UUID:
        await start(self.book)
        return await seed_unknown(self.book)

    async def observe(self, offers: tuple[str, ...] = ()) -> str:
        clock = Clock()
        venue = FakeVenue(clock, offers=tuple(venue_offer(item) for item in offers))
        result = await run_cycle(self.book, venue, clock, self.at)
        self.at += 100
        assert result.observation_id is not None and result.resolutions == ()
        return observation_evidence_ref(result.observation_id)

    def other_authority_ref(self) -> str:
        return "42"

    def another_ref(self) -> str:
        return observation_evidence_ref(uuid4())


class LegacyDriver(Driver):
    name = "legacy"

    def __init__(self, stack: Stack) -> None:
        super().__init__(stack)
        self.resolution = LegacyOperatorResolution()
        self.reads = LegacyOperatorReads()
        self.at = 2_000

    async def open_unknown(self) -> UUID:
        from tests.modules.api.test_uncertainties_router import SCID, _snapshot

        reference = ReservationRef(
            execution_decision_id="decision-op", cid=7, signal_correlation_id=SCID
        )
        async with self.factory.begin() as session:
            session.add(ExecutionDecisionRow(
                decision_id="decision-op", account_id=str(ACCOUNT), exchange_account_id=ACCOUNT,
                deployment_environment=ENVIRONMENT, reconcile_id="op", cell_id="cell-1",
                symbol="fUST", signal_correlation_id=str(SCID), outcome="ready",
                signal_rate=Decimal("0.001"), applied_rate=Decimal("0.001"),
                amount_usdt=Decimal("100"), duration_days=2, model_evidence={},
                safety_result={}, execution_policy="standard", service_version="test",
                config_hash="test", occurred_at_ms=900, recorded_at_ms=900,
            ))
            await session.flush()
            store = PostgresEventStore(deployment_environment=ENVIRONMENT)
            await store.append_snapshot(session, _snapshot(ACCOUNT, finished_at=1000))
            await store.append(session, ReservationIntent(
                symbol="fUST", cid=7, signal_correlation_id=SCID, account_id=str(ACCOUNT),
                is_simulated=True, execution_decision_id="decision-op",
                reservation_ref=reference,
                submission_attempt=SubmissionAttemptPayload(
                    execution_decision_id="decision-op", account_id=ACCOUNT,
                    environment=ENVIRONMENT, symbol="fUST", cid=7,
                    normalized_payload={
                        "type": "LIMIT", "symbol": "fUST", "amount": "100", "rate": "0.001",
                        "period": 2, "flags": 0,
                    },
                    started_at_ms=1000,
                ),
                amount=Decimal("100"), occurred_at_ms=1050,
            ))
            await store.append(session, ReservationUnknown(
                symbol="fUST", cid=7, size_usdt=Decimal("100"), signal_correlation_id=SCID,
                account_id=str(ACCOUNT), is_simulated=True, reason="connection_reset",
                occurred_at_ms=1100, reservation_ref=reference,
            ))
        async with self.factory() as session:
            found = await session.scalar(select(ExecutionUncertaintyRow.uncertainty_id))
        assert found is not None
        return found

    async def observe(self, offers: tuple[str, ...] = ()) -> str:
        from tests.modules.api.test_uncertainties_router import _snapshot

        observed = tuple(
            VenueOfferObservation(
                item, "fUST", Decimal("100"), Decimal("100"), Decimal("0.001"), 2, "active",
                1_500, 1_500, offer_type="LIMIT", flags={"raw": 0},
            )
            for item in offers
        )
        async with self.factory.begin() as session:
            await PostgresEventStore(deployment_environment=ENVIRONMENT).append_snapshot(
                session,
                _snapshot(ACCOUNT, finished_at=self.at, started_at=self.at - 10, offers=observed),
            )
        self.at += 1_000
        async with self.factory() as session:
            seq = await session.scalar(
                select(func.max(EventLogRow.event_seq)).where(
                    EventLogRow.event_type == "VENUE_SNAPSHOT_OBSERVED"
                )
            )
        return str(seq)

    def other_authority_ref(self) -> str:
        return "ledger:v1:obs:00000000-0000-0000-0000-0000000000aa"

    def another_ref(self) -> str:
        return "999999"


def build_driver(stack: Stack) -> Driver:
    return LedgerDriver(stack) if stack.name == "ledger" else LegacyDriver(stack)
