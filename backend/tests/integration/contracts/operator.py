"""Operator-request scenarios over the ledger stack.

One UNKNOWN submit, the observations that follow it, and the port a request goes
through; the driver writes through the ledger's paths (the journal and the observation
cycle), at times that keep the attempt inside its settle window so only an operator
resolves it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from bfx_funding_bot.modules.execution.uncertainty_requests import (
    ResolutionScope,
    UncertaintyResolutionRequests,
    UncertaintyResolutionWorker,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    UncertaintyResolutionRequestRow,
)
from bfx_funding_bot.modules.ledger import (
    OperatorEvidence,
    OperatorReads,
    OperatorResolution,
    ResolutionEvidence,
    ResolutionIntent,
    ResolutionSubject,
    observation_evidence_ref,
)
from bfx_funding_bot.modules.ledger.wiring import (
    build_operator_evidence,
    build_operator_reads,
    build_operator_resolution,
)

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
    evidence: OperatorEvidence

    def __init__(self, stack: Stack) -> None:
        self.stack = stack
        self.factory: Any = stack.factory

    async def open_unknown(self) -> UUID:
        raise NotImplementedError

    async def observe(self, offers: tuple[str, ...] = ()) -> str:
        """A newer, complete observation naming ``offers`` (matching the UNKNOWN); its ref."""
        raise NotImplementedError

    def other_authority_ref(self) -> str:
        """A well-formed reference of another shape (the frozen legacy event sequence)."""
        raise NotImplementedError

    def another_ref(self) -> str:
        """A well-formed reference of this authority that is not the one observed."""
        raise NotImplementedError

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

    async def context(self, uncertainty: UUID) -> ResolutionEvidence:
        """What the operator may cite for this subject now."""
        view = await self.view(uncertainty)
        subject = ResolutionSubject(view.uncertainty_id, view.symbol, view.attempt_id)
        async with self.factory() as session:
            return await self.evidence.resolution_context(session, SCOPE, subject)


class LedgerDriver(Driver):
    name = "ledger"

    def __init__(self, stack: Stack) -> None:
        super().__init__(stack)
        self.resolution = build_operator_resolution()
        self.reads = build_operator_reads()
        self.evidence = build_operator_evidence()
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


def build_driver(stack: Stack) -> Driver:
    return LedgerDriver(stack)
