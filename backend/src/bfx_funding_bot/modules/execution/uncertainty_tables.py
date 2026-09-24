"""Durable submit-attempt and scoped uncertainty projections.

These rows record immutable submit identity and the current, rebuildable
uncertainty projection.  The event writer remains the only production caller
that should project them; keeping the tables separately importable lets unit
fixtures exercise their PostgreSQL constraints on SQLite too.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, get_args
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON, Uuid

from bfx_funding_bot.core.db import Base

# Register the referenced safety table whenever this module is imported.  A
# number of SQLite metadata fixtures import uncertainty tables directly; the
# FK is real and must remain visible to SQLAlchemy's create_all as well as
# Alembic's complete metadata import.
from bfx_funding_bot.modules.execution.safety.tables import TradingHaltRow  # noqa: F401

_JSON = JSON().with_variant(JSONB, "postgresql")
_UUID = PG_UUID(as_uuid=True).with_variant(Uuid(as_uuid=True), "sqlite")
_NOW = func.current_timestamp()


class SubmissionAttemptRow(Base):
    """One immutable venue-write identity per execution decision."""

    __tablename__ = "submission_attempts"

    attempt_id: Mapped[UUID] = mapped_column(
        _UUID,
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    execution_decision_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(
            "execution_decisions.decision_id",
            ondelete="RESTRICT",
            name="fk_submission_attempts_execution_decision",
        ),
        nullable=False,
        unique=True,
    )
    exchange_account_id: Mapped[UUID] = mapped_column(
        _UUID,
        ForeignKey(
            "exchange_accounts.id",
            ondelete="RESTRICT",
            name="fk_submission_attempts_account",
        ),
        nullable=False,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    cid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    started_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completed_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    outcome_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    venue_offer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_event_seq: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "event_log.event_seq",
            ondelete="RESTRICT",
            name="fk_submission_attempts_last_event",
        ),
        nullable=True,
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )

    __table_args__ = (
        CheckConstraint("cid >= 0", name="ck_submission_attempts_cid_nonnegative"),
        CheckConstraint(
            "started_at_ms >= 0 AND (completed_at_ms IS NULL OR completed_at_ms >= started_at_ms)",
            name="ck_submission_attempts_timestamps",
        ),
        CheckConstraint(
            "last_event_seq IS NULL OR last_event_seq >= 0",
            name="ck_submission_attempts_event_seq_nonnegative",
        ),
        CheckConstraint(
            "outcome_kind IS NULL OR outcome_kind IN "
            "('acknowledged', 'rejected', 'unknown', 'not_sent')",
            name="ck_submission_attempts_outcome_kind",
        ),
        Index(
            "idx_submission_attempts_scope",
            "exchange_account_id",
            "deployment_environment",
            "symbol",
        ),
    )


class CanaryCommandPermitRow(Base):
    """One durable permission for one canary command under one halt epoch.

    The halt row is intentionally part of the identity.  A permit can be
    consumed at most once, and a second permit cannot be issued for the same
    append-only halt transition even after the first command is complete.
    Release-session max_amount/expiry and measured binding live in release_sessions;
    this amount is the immutable exact decision amount consumed before intent IO.
    Migration b4e6f8a0c203 protects both legacy and session permit audit in place.
    """

    __tablename__ = "canary_command_permits"

    permit_id: Mapped[UUID] = mapped_column(
        _UUID,
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    halt_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("trading_halt.id", ondelete="RESTRICT", name="fk_canary_permits_halt"),
        nullable=False,
        unique=True,
    )
    exchange_account_id: Mapped[UUID] = mapped_column(
        _UUID,
        ForeignKey(
            "exchange_accounts.id",
            ondelete="RESTRICT",
            name="fk_canary_permits_account",
        ),
        nullable=False,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    cell: Mapped[str] = mapped_column(Text, nullable=False)
    strategy: Mapped[str] = mapped_column(Text, nullable=False)
    amount_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    operator_id: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'issued'"))
    issued_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    consumed_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    execution_decision_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey(
            "execution_decisions.decision_id",
            ondelete="RESTRICT",
            name="fk_canary_permits_decision",
        ),
        nullable=True,
    )
    attempt_id: Mapped[UUID | None] = mapped_column(
        _UUID,
        ForeignKey(
            "submission_attempts.attempt_id",
            ondelete="RESTRICT",
            name="fk_canary_permits_attempt",
        ),
        nullable=True,
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('issued', 'consumed')",
            name="ck_canary_permits_state",
        ),
        CheckConstraint("amount_usdt > 0", name="ck_canary_permits_amount_positive"),
        CheckConstraint(
            "issued_at_ms >= 0 AND (consumed_at_ms IS NULL OR consumed_at_ms >= issued_at_ms)",
            name="ck_canary_permits_timestamps",
        ),
        CheckConstraint(
            "(state = 'issued' AND consumed_at_ms IS NULL) OR "
            "(state = 'consumed' AND consumed_at_ms IS NOT NULL)",
            name="ck_canary_permits_state_timestamp",
        ),
        Index(
            "idx_canary_permits_scope",
            "exchange_account_id",
            "deployment_environment",
            "state",
        ),
    )


class ExecutionUncertaintyRow(Base):
    """Current account/symbol block caused by unresolved venue evidence."""

    __tablename__ = "execution_uncertainties"

    uncertainty_id: Mapped[UUID] = mapped_column(
        _UUID,
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    exchange_account_id: Mapped[UUID] = mapped_column(
        _UUID,
        ForeignKey(
            "exchange_accounts.id",
            ondelete="RESTRICT",
            name="fk_execution_uncertainties_account",
        ),
        nullable=False,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_key: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    intended_amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    attempt_id: Mapped[UUID | None] = mapped_column(
        _UUID,
        ForeignKey(
            "submission_attempts.attempt_id",
            ondelete="RESTRICT",
            name="fk_execution_uncertainties_attempt",
        ),
        nullable=True,
    )
    venue_offer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    opened_event_seq: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "event_log.event_seq",
            ondelete="RESTRICT",
            name="fk_execution_uncertainties_opened_event",
        ),
        nullable=False,
    )
    reconcile_event_seq: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "event_log.event_seq",
            ondelete="RESTRICT",
            name="fk_execution_uncertainties_reconcile_event",
        ),
        nullable=True,
    )
    resolved_event_seq: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "event_log.event_seq",
            ondelete="RESTRICT",
            name="fk_execution_uncertainties_resolved_event",
        ),
        nullable=True,
    )
    resolved_by_operator_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_evidence: Mapped[dict[str, Any] | None] = mapped_column(_JSON, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('submit_outcome_unknown', 'unattributed_venue_offer', "
            "'unsupported_venue_exposure')",
            name="ck_execution_uncertainties_kind",
        ),
        CheckConstraint(
            "state IN ('open', 'resolved')",
            name="ck_execution_uncertainties_state",
        ),
        CheckConstraint(
            "intended_amount >= 0",
            name="ck_execution_uncertainties_intended_amount_nonnegative",
        ),
        CheckConstraint(
            "(state = 'open' AND reconcile_event_seq IS NULL AND resolved_event_seq IS NULL "
            "AND resolved_by_operator_id IS NULL AND resolution_reason IS NULL "
            "AND resolution_evidence IS NULL AND resolved_at IS NULL) OR "
            "(state = 'resolved' AND reconcile_event_seq IS NOT NULL AND resolved_event_seq IS NOT NULL "
            "AND resolved_by_operator_id IS NOT NULL AND resolution_reason IS NOT NULL "
            "AND resolution_evidence IS NOT NULL AND resolved_at IS NOT NULL)",
            name="ck_execution_uncertainties_resolution_shape",
        ),
        CheckConstraint(
            "state = 'open' OR (opened_event_seq < reconcile_event_seq "
            "AND reconcile_event_seq < resolved_event_seq)",
            name="ck_execution_uncertainties_resolution_event_order",
        ),
        CheckConstraint(
            "(kind = 'unattributed_venue_offer' AND venue_offer_id IS NOT NULL) OR "
            "(kind IN ('submit_outcome_unknown', 'unsupported_venue_exposure') "
            "AND venue_offer_id IS NULL)",
            name="ck_execution_uncertainties_venue_link_kind",
        ),
        ForeignKeyConstraint(
            ["exchange_account_id", "deployment_environment", "venue_offer_id"],
            [
                "venue_offer_state.exchange_account_id",
                "venue_offer_state.deployment_environment",
                "venue_offer_state.venue_offer_id",
            ],
            name="fk_execution_uncertainties_venue_offer",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_execution_uncertainties_correlation",
            "exchange_account_id",
            "deployment_environment",
            "symbol",
            "kind",
            "correlation_key",
            unique=True,
        ),
        Index(
            "uq_execution_uncertainties_open_scope",
            "exchange_account_id",
            "deployment_environment",
            "symbol",
            "kind",
            unique=True,
            postgresql_where=text("state = 'open'"),
            sqlite_where=text("state = 'open'"),
        ),
    )


# Single source for the action set: the Literal types the code, the tuple
# builds the CHECK below, and a test pins the migration to both.
ResolutionAction = Literal["bind_to_venue", "mark_not_accepted", "manual_resolution"]
UNCERTAINTY_RESOLUTION_ACTIONS: tuple[str, ...] = get_args(ResolutionAction)
ResolutionRequestState = Literal["requested", "applied", "rejected", "failed"]
UNCERTAINTY_REQUEST_STATES: tuple[str, ...] = get_args(ResolutionRequestState)
# Column sets for the migration's column-scoped grants and for the explicit
# INSERT the web API issues: an ORM flush would also send the worker-only
# columns as NULL and trip the webapi ACL. A test pins the migration's grant
# strings and this table's columns to these tuples.
UNCERTAINTY_REQUEST_COLUMNS = (
    "request_id", "exchange_account_id", "deployment_environment", "uncertainty_id",
    "action", "reconcile_event_seq", "venue_offer_id", "decision", "reason",
    "requested_by", "created_at_ms",
)
UNCERTAINTY_WORKER_COLUMNS = ("state", "processed_at_ms", "resolved_event_seq", "outcome_reason")


class UncertaintyResolutionRequestRow(Base):
    """Operator adjudication handed to the account daemon (ADR D4').

    Not a projection: the web API only inserts the request columns and the
    daemon's resolution worker alone appends the resolution event, so the web
    API needs no ledger or projection write privilege. ``uncertainty_id`` is
    deliberately not a foreign key -- ``execution_uncertainties`` is rebuilt
    from the event log and must stay free to be.
    """

    __tablename__ = "uncertainty_resolution_requests"

    request_id: Mapped[UUID] = mapped_column(_UUID, primary_key=True)
    exchange_account_id: Mapped[UUID] = mapped_column(
        _UUID,
        ForeignKey(
            "exchange_accounts.id",
            ondelete="RESTRICT",
            name="fk_uncertainty_resolution_requests_account",
        ),
        nullable=False,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    uncertainty_id: Mapped[UUID] = mapped_column(_UUID, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    reconcile_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    venue_offer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Worker-only from here down.
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'requested'")
    )
    processed_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resolved_event_seq: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "event_log.event_seq",
            ondelete="RESTRICT",
            name="fk_uncertainty_resolution_requests_event",
        ),
        nullable=True,
    )
    outcome_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"action IN ({', '.join(repr(a) for a in UNCERTAINTY_RESOLUTION_ACTIONS)})",
            name="ck_uncertainty_resolution_requests_action",
        ),
        CheckConstraint(
            f"state IN ({', '.join(repr(s) for s in UNCERTAINTY_REQUEST_STATES)})",
            name="ck_uncertainty_resolution_requests_state",
        ),
        CheckConstraint(
            "(action = 'bind_to_venue' AND venue_offer_id IS NOT NULL AND decision IS NULL) OR "
            "(action = 'mark_not_accepted' AND venue_offer_id IS NULL AND decision IS NULL) OR "
            "(action = 'manual_resolution' AND venue_offer_id IS NULL AND decision IS NOT NULL "
            "AND reason IS NOT NULL)",
            name="ck_uncertainty_resolution_requests_action_shape",
        ),
        CheckConstraint(
            "(state = 'requested' AND processed_at_ms IS NULL AND resolved_event_seq IS NULL "
            "AND outcome_reason IS NULL) OR "
            "(state = 'applied' AND processed_at_ms IS NOT NULL AND resolved_event_seq IS NOT NULL "
            "AND outcome_reason IS NULL) OR "
            "(state IN ('rejected', 'failed') AND processed_at_ms IS NOT NULL "
            "AND resolved_event_seq IS NULL AND outcome_reason IS NOT NULL)",
            name="ck_uncertainty_resolution_requests_outcome_shape",
        ),
        Index(
            "uq_uncertainty_resolution_requests_pending",
            "exchange_account_id",
            "deployment_environment",
            "uncertainty_id",
            unique=True,
            postgresql_where=text("state = 'requested'"),
            sqlite_where=text("state = 'requested'"),
        ),
        Index(
            "ix_uncertainty_resolution_requests_queue",
            "exchange_account_id",
            "deployment_environment",
            "state",
            "created_at_ms",
        ),
    )
