"""ReservationEmittingMiddleware — ExecutorPort wrapper over the account command gate.

Every submit and cancel goes through the serialized ``AccountCommandGate``: the
write-ahead intent, the venue call and the terminal outcome are recorded by its
``CommandBoundary``, so there is no persister-only path.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from uuid import UUID

from bfx_funding_bot.modules.execution.command_boundary import CommandBoundary
from bfx_funding_bot.modules.execution.command_gate import (
    AccountCommandGate,
    AuthoritativeSafetyEvaluator,
)
from bfx_funding_bot.modules.execution.contracts import ReadyToSubmit, ReservationRef
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    ExecutorPort,
    SubmittedOrder,
)
from bfx_funding_bot.modules.ledger import ManagedOfferReader, UncertaintyReader


class ReservationEmittingMiddleware:
    """ExecutorPort wrapper: every command passes the account command gate."""

    def __init__(
        self,
        inner: ExecutorPort,
        *,
        safety_evaluator: AuthoritativeSafetyEvaluator,
        boundary: CommandBoundary,
        uncertainty_reader: UncertaintyReader,
        managed_offers: ManagedOfferReader,
        clock: Callable[[], int] | None = None,
        date_provider: Callable[[], date] | None = None,
    ) -> None:
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._date_provider = date_provider or (lambda: datetime.now(UTC).date())
        self._command_gate = AccountCommandGate(
            inner,
            uncertainty_reader=uncertainty_reader,
            safety_evaluator=safety_evaluator,
            deployment_environment=boundary.scope.deployment_environment,
            boundary=boundary,
            managed_offers=managed_offers,
            clock=self._clock,
            date_provider=self._date_provider,
        )

    @property
    def command_gate(self) -> AccountCommandGate:
        """Same daemon writer boundary used by an explicitly authorized release session."""
        return self._command_gate

    async def cancel(self, *, venue_offer_id: str, signal_correlation_id: UUID,
                     account_id: str, ctx: AccountContext) -> None:
        await self._command_gate.cancel(venue_offer_id=venue_offer_id,
            signal_correlation_id=signal_correlation_id, account_id=account_id, ctx=ctx)

    async def submit(
        self, ready: ReadyToSubmit, ctx: AccountContext, *, cid: int | None = None,
        reservation_ref: ReservationRef | None = None,
    ) -> SubmittedOrder:
        return await self._command_gate.submit(
            ready, ctx, cid=cid, reservation_ref=reservation_ref,
        )
