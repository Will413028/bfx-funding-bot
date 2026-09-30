"""Dormant, transaction-scoped ledger journal and capital-read contracts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.trading import CapitalResult, CapitalScope

type JsonObject = dict[str, object]
type OutcomeKind = Literal["ack", "rejected", "not_sent", "unknown"]
type ResolutionAction = Literal["bound_to_venue", "not_accepted", "manual"]


@dataclass(frozen=True, slots=True)
class Scope:
    exchange_account_id: UUID
    deployment_environment: str


@dataclass(frozen=True, slots=True)
class QueryHandle:
    query_id: UUID
    query_revision: int
    start_revision: int
    started_at_ms: int


@dataclass(frozen=True, slots=True)
class Attempt:
    attempt_id: UUID
    execution_decision_id: str
    symbol: str
    cell_id: str  # must equal the decision's cell (enforced by the scope trigger)
    normalized_payload: JsonObject
    basis_id: UUID
    policy_revision_id: UUID
    authorization_evidence: JsonObject
    started_at_ms: int
    seed_provenance: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class RecordedAttempt:
    attempt_id: UUID
    attempt_seq: int
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class Outcome:
    attempt_id: UUID
    kind: OutcomeKind
    venue_offer_id: str | None
    reason: str | None
    completed_at_ms: int
    evidence: JsonObject


@dataclass(frozen=True, slots=True)
class Resolution:
    id: UUID
    symbol: str
    action: ResolutionAction
    venue_offer_id: str | None
    observation_id: UUID
    actor_kind: str
    actor_id: str
    resolved_at_ms: int
    reason: str
    evidence: JsonObject
    attempt_id: UUID | None = None
    quarantine_id: UUID | None = None
    operator_request_id: UUID | None = None
    candidate_count: int | None = None


@dataclass(frozen=True, slots=True)
class Quarantine:
    quarantine_id: UUID
    symbol: str
    intended_amount: Decimal
    opened_at_ms: int
    evidence: JsonObject
    legacy_reconcile_event_seq: int | None = None


@dataclass(frozen=True, slots=True)
class QuarantineMember:
    quarantine_id: UUID
    source_kind: Literal["offer", "credit", "loan"]
    venue_object_id: str
    observation_id: UUID
    amount_at_join: Decimal


class OutcomeAlreadyRecorded(ValueError):  # noqa: N818 - named by journal contract
    def __init__(self, stored: Outcome) -> None:
        self.stored = stored
        super().__init__("outcome already recorded")


class ResolutionAlreadyRecorded(ValueError):  # noqa: N818 - named by journal contract
    def __init__(self, stored: Resolution) -> None:
        self.stored = stored
        super().__init__("resolution already recorded")


class QuarantineMemberConflict(ValueError):  # noqa: N818 - named by journal contract
    def __init__(self, stored: QuarantineMember) -> None:
        self.stored = stored
        super().__init__("quarantine member already recorded")


class ResolutionRejected(ValueError):  # noqa: N818 - named by journal contract
    """Structural subject or observation precondition failed."""


class QueryAdmissionRefused(ValueError):  # noqa: N818 - named by query contract
    """An in-flight command makes a pre-I/O fence unsafe."""


class LedgerJournal(Protocol):
    async def bump_clock(self, session: AsyncSession, scope: Scope) -> int: ...
    async def begin_query(
        self, session: AsyncSession, scope: Scope, started_at_ms: int
    ) -> QueryHandle: ...
    async def record_attempt(
        self, session: AsyncSession, scope: Scope, attempt: Attempt
    ) -> RecordedAttempt: ...
    async def record_outcome(
        self, session: AsyncSession, scope: Scope, outcome: Outcome
    ) -> None: ...
    async def read_back_outcome(
        self, session: AsyncSession, attempt_id: UUID
    ) -> Outcome | None: ...
    async def record_resolution(
        self, session: AsyncSession, scope: Scope, resolution: Resolution
    ) -> None: ...
    async def open_quarantine(
        self, session: AsyncSession, scope: Scope, quarantine: Quarantine
    ) -> None: ...
    async def add_quarantine_member(
        self, session: AsyncSession, scope: Scope, member: QuarantineMember
    ) -> None: ...


class CapitalReadRefused(ValueError):  # noqa: N818 - named by reader contract
    """The caller's transaction is not REPEATABLE READ READ ONLY."""


@dataclass(frozen=True, slots=True)
class LedgerCapitalRead:
    """``trading.derive_capital``'s result and the basis it folded (None if none)."""

    result: CapitalResult
    basis_id: UUID | None


class LedgerCapitalReader(Protocol):
    """Read one symbol/cell's capital from the latest accepted basis and its tail.

    The caller opens and holds one REPEATABLE READ READ ONLY transaction on
    ``session`` (anything else raises ``CapitalReadRefused``); ``now_ms`` is
    the caller's local clock, compared with the query's local start time.
    """

    async def read_capital(
        self,
        session: AsyncSession,
        scope: CapitalScope,
        *,
        now_ms: int,
        max_snapshot_age_ms: int,
    ) -> LedgerCapitalRead: ...


__all__ = [
    "Attempt",
    "CapitalReadRefused",
    "JsonObject",
    "LedgerCapitalRead",
    "LedgerCapitalReader",
    "LedgerJournal",
    "Outcome",
    "OutcomeAlreadyRecorded",
    "OutcomeKind",
    "Quarantine",
    "QuarantineMember",
    "QuarantineMemberConflict",
    "QueryAdmissionRefused",
    "QueryHandle",
    "RecordedAttempt",
    "Resolution",
    "ResolutionAction",
    "ResolutionAlreadyRecorded",
    "ResolutionRejected",
    "Scope",
]
