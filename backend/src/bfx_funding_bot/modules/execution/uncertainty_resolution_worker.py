"""The uncertainty-adjudication worker: applies queued resolutions in the account daemon.

The request side -- validating and queueing an adjudication, shared with the web API --
is ``execution.uncertainty_requests``; this worker re-runs that validation
authoritatively under the account lock and applies the ledger's ``OperatorResolution``
in the same transaction (ADR D4').
"""


from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.operator_request_worker import (
    OperatorAuthority,
    OperatorRequestWorker,
    Outcome,
    root_cause_name,
)
from bfx_funding_bot.modules.execution.operator_requests import APPLIED
from bfx_funding_bot.modules.execution.uncertainty_requests import (
    ResolutionScope,
    UncertaintyResolutionRequests,
    intent_from_request,
    request_rejection,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    UncertaintyResolutionRequestRow,
)
from bfx_funding_bot.modules.ledger import (
    OperatorResolution,
    QueuedResolution,
    RequestColumns,
    Scope,
)
from bfx_funding_bot.modules.ledger import (
    ResolutionRejected as EvidenceRejected,
)


class UncertaintyResolutionWorker(OperatorRequestWorker[UncertaintyResolutionRequestRow, None]):
    """Applies queued adjudications inside the account's single writer."""

    model = UncertaintyResolutionRequestRow
    name = "uncertainty_resolution"

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        scope: ResolutionScope,
        authority: OperatorAuthority,
        clock: Callable[[], int] | None = None,
        ownership: Callable[[], Awaitable[bool]] | None = None,
        poll_interval_s: float = 2.0,
        resolution: OperatorResolution,
    ) -> None:
        super().__init__(session_factory=session_factory, account_id=scope.account_id,
                         environment=scope.environment, authority=authority, clock=clock,
                         ownership=ownership, poll_interval_s=poll_interval_s)
        self.scope = scope
        self.requests = UncertaintyResolutionRequests(scope, resolution)

    async def apply(self, session: AsyncSession, row: UncertaintyResolutionRequestRow,
                    prepared: None) -> Outcome:
        try:
            await self.requests.resolution.apply(
                session, Scope(self.scope.account_id, self.scope.environment),
                QueuedResolution(
                    row.request_id, intent_from_request(row),
                    RequestColumns(row.observation_id),
                ),
                now_ms=self.clock(),
            )
        except EvidenceRejected as exc:
            raise request_rejection(exc) from exc
        return Outcome(APPLIED)

    def failure_reason(self, exc: BaseException) -> str:
        return "resolution_failed:" + root_cause_name(exc)


__all__ = ["UncertaintyResolutionWorker"]
