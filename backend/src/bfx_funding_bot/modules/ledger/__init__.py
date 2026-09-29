"""Dormant, transaction-scoped ledger journal contracts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

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


__all__ = [
    "Attempt",
    "JsonObject",
    "LedgerJournal",
    "Outcome",
    "OutcomeAlreadyRecorded",
    "OutcomeKind",
    "Quarantine",
    "QuarantineMember",
    "QuarantineMemberConflict",
    "QueryHandle",
    "RecordedAttempt",
    "Resolution",
    "ResolutionAction",
    "ResolutionAlreadyRecorded",
    "ResolutionRejected",
    "Scope",
]
