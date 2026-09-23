"""Last-submit-attempt slot — the money path's most recent observable action.

The DeploymentReconciler already logs every outcome of a submit attempt
(``deployment_skip`` / ``deployment_submitted`` / ``deployment_submit_rejected``
/ ``deployment_submit_error``). Those lines are the right evidence at the right
place — they were simply unqueryable, so answering "is the bot placing orders
right now?" meant grepping container logs and inferring.

This records the same outcomes into a slot ``GET /admin/trading-status`` reads.
Process-local and in-memory on purpose: the question it answers is about THIS
running daemon, and it must stay answerable when the database is the thing
that's unhealthy. ``started_at`` is therefore part of the contract — a null
attempt means "nothing tried since this process started", never "never traded".

Only the latest attempt is kept. History belongs in event_log; this is the
"what is happening now" view, and a bounded history here would invite reading
it as an audit trail it is not.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

# blocked  = the guard chain refused it (never left the process)
# submitted = the venue accepted it
# rejected = guards passed, the venue refused it (e.g. 10001 insufficient balance)
# error    = the submit call raised
#
# `blocked` vs `rejected` must never be collapsed: a venue rejection means the
# safety chain PASSED and an offer really was sent — which reads as a working
# halt if mislabelled.
SubmitOutcome = Literal["blocked", "submitted", "rejected", "error"]


@dataclass(frozen=True, slots=True)
class SubmitAttempt:
    at: datetime
    cell: str
    symbol: str
    amount: Decimal
    outcome: SubmitOutcome
    guard_name: str | None = None
    reason: str | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SubmitAttemptRecorder:
    def __init__(self, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self.started_at = clock()
        self._last: SubmitAttempt | None = None

    @property
    def last(self) -> SubmitAttempt | None:
        return self._last

    def record_blocked(
        self, *, cell: str, symbol: str, amount: Decimal,
        guard_name: str, reason: str | None,
    ) -> None:
        self._record(
            cell=cell, symbol=symbol, amount=amount, outcome="blocked",
            guard_name=guard_name, reason=reason,
        )

    def record_submitted(self, *, cell: str, symbol: str, amount: Decimal) -> None:
        self._record(cell=cell, symbol=symbol, amount=amount, outcome="submitted")

    def record_rejected(
        self, *, cell: str, symbol: str, amount: Decimal, reason: str | None,
    ) -> None:
        self._record(
            cell=cell, symbol=symbol, amount=amount, outcome="rejected", reason=reason,
        )

    def record_error(
        self, *, cell: str, symbol: str, amount: Decimal, reason: str | None,
    ) -> None:
        self._record(
            cell=cell, symbol=symbol, amount=amount, outcome="error", reason=reason,
        )

    def _record(
        self, *, cell: str, symbol: str, amount: Decimal, outcome: SubmitOutcome,
        guard_name: str | None = None, reason: str | None = None,
    ) -> None:
        self._last = SubmitAttempt(
            at=self._clock(), cell=cell, symbol=symbol, amount=amount,
            outcome=outcome, guard_name=guard_name, reason=reason,
        )

    def as_dict(self) -> dict[str, str | None] | None:
        """JSON-safe view, or None when nothing has been attempted yet.

        `amount` is stringified rather than floated — this is a real-money
        figure in a report an operator reads before deciding to intervene.
        """
        a = self._last
        if a is None:
            return None
        return {
            "at": a.at.isoformat(),
            "cell": a.cell,
            "symbol": a.symbol,
            "amount": str(a.amount),
            "outcome": a.outcome,
            "guard_name": a.guard_name,
            "reason": a.reason,
        }
