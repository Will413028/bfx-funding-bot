"""Durable submit-attempt and scoped uncertainty projections.

These rows record immutable submit identity and the current, rebuildable
uncertainty projection.  The event writer remains the only production caller
that should project them; keeping the tables separately importable lets unit
fixtures exercise their PostgreSQL constraints on SQLite too.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
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
    execution_decision_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
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
            "(state = 'open' AND resolved_event_seq IS NULL "
            "AND resolved_by_operator_id IS NULL AND resolution_reason IS NULL "
            "AND resolution_evidence IS NULL AND resolved_at IS NULL) OR "
            "(state = 'resolved' AND resolved_event_seq IS NOT NULL "
            "AND resolved_by_operator_id IS NOT NULL AND resolution_reason IS NOT NULL "
            "AND resolution_evidence IS NOT NULL AND resolved_at IS NOT NULL)",
            name="ck_execution_uncertainties_resolution_shape",
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
