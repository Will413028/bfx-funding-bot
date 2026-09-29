"""Ledger S1-1 tables; all new facts are append-only except the clock and mirrors."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, MappedColumn, mapped_column

from bfx_funding_bot.core.db import Base

_JSON = JSON().with_variant(JSONB, "postgresql")


def _account() -> MappedColumn[UUID]:
    return mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )


class CapitalCommandClockRow(Base):
    __tablename__ = "capital_command_clock"
    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    __table_args__ = (CheckConstraint("revision >= 0", name="ck_capital_command_clock_revision"),)


class LedgerObservationRow(Base):
    __tablename__ = "ledger_observation"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    query_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, unique=True)
    exchange_account_id: Mapped[UUID] = _account()
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    query_started_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    query_finished_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    confirmation_finished_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    start_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    accept_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    wallets_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    offers_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    credits_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    loans_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    offer_history_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    credit_history_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    history_requested_start_ms: Mapped[int | None] = mapped_column(BigInteger)
    history_requested_end_ms: Mapped[int | None] = mapped_column(BigInteger)
    history_oldest_mts_created: Mapped[int | None] = mapped_column(BigInteger)
    history_newest_mts_created: Mapped[int | None] = mapped_column(BigInteger)
    first_digest: Mapped[str] = mapped_column(Text, nullable=False)
    confirmation_digest: Mapped[str] = mapped_column(Text, nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    __table_args__ = (
        UniqueConstraint("id", "accepted", name="uq_ledger_observation_accepted"),
        CheckConstraint(
            "schema_version >= 1 AND query_started_at_ms >= 0 AND query_finished_at_ms >= "
            "query_started_at_ms AND confirmation_finished_at_ms >= query_finished_at_ms "
            "AND start_revision >= 0 AND accept_revision >= 0",
            name="ck_ledger_observation_order",
        ),
        CheckConstraint(
            "NOT accepted OR (start_revision = accept_revision AND wallets_complete AND "
            "offers_complete AND credits_complete AND loans_complete AND "
            "offer_history_complete AND credit_history_complete)",
            name="ck_ledger_observation_acceptance",
        ),
        CheckConstraint(
            "(history_requested_start_ms IS NULL) = (history_requested_end_ms IS NULL) "
            "AND (history_requested_start_ms IS NULL OR (history_requested_start_ms >= 0 "
            "AND history_requested_end_ms >= history_requested_start_ms)) AND "
            "(history_oldest_mts_created IS NULL) = (history_newest_mts_created IS NULL) "
            "AND (history_oldest_mts_created IS NULL OR (history_oldest_mts_created >= 0 "
            "AND history_newest_mts_created >= history_oldest_mts_created))",
            name="ck_ledger_observation_history_range",
        ),
        CheckConstraint(
            "first_digest = confirmation_digest", name="ck_ledger_observation_matching_digest"
        ),
        Index(
            "ix_ledger_observation_scope_started",
            "exchange_account_id",
            "deployment_environment",
            text("query_started_at_ms DESC"),
            "id",
        ),
    )


class LedgerObservationWalletRow(Base):
    __tablename__ = "ledger_observation_wallet"
    observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    wallet_type: Mapped[str] = mapped_column(Text, primary_key=True)
    currency: Mapped[str] = mapped_column(Text, primary_key=True)
    available: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    __table_args__ = (
        CheckConstraint("available >= 0 AND balance >= 0", name="ck_ledger_wallet_amount"),
    )


class LedgerObservationOfferRow(Base):
    __tablename__ = "ledger_observation_offer"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    venue_offer_id: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    amount_original: Mapped[Decimal | None] = mapped_column(Numeric)
    amount_remaining: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    rate: Mapped[Decimal | None] = mapped_column(Numeric)
    rate_observed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    period_days: Mapped[int | None] = mapped_column(Integer)
    offer_type: Mapped[str | None] = mapped_column(Text)
    flags: Mapped[dict[str, Any] | int | None] = mapped_column(_JSON)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    mts_created: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mts_updated: Mapped[int | None] = mapped_column(BigInteger)
    raw: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "observation_id", "venue_offer_id", name="uq_ledger_observation_offer_venue"
        ),
        CheckConstraint(
            "amount_remaining >= 0 AND (amount_original IS NULL OR amount_original >= 0) "
            "AND (rate IS NULL OR rate >= 0) AND (period_days IS NULL OR period_days > 0) "
            "AND mts_created >= 0 AND (mts_updated IS NULL OR mts_updated >= 0)",
            name="ck_ledger_observation_offer_amount",
        ),
    )


class LedgerObservationCreditRow(Base):
    __tablename__ = "ledger_observation_credit"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    venue_credit_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    rate: Mapped[Decimal | None] = mapped_column(Numeric)
    period_days: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    flags: Mapped[dict[str, Any] | int | None] = mapped_column(_JSON)
    mts_created: Mapped[int | None] = mapped_column(BigInteger)
    mts_updated: Mapped[int | None] = mapped_column(BigInteger)
    mts_opening: Mapped[int | None] = mapped_column(BigInteger)
    raw: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "observation_id",
            "venue_credit_id",
            "source_kind",
            name="uq_ledger_observation_credit_venue",
        ),
        CheckConstraint(
            "source_kind IN ('credit','loan')", name="ck_ledger_observation_credit_source"
        ),
        CheckConstraint(
            "amount >= 0 AND (rate IS NULL OR rate >= 0) AND (period_days IS NULL OR "
            "period_days > 0) AND (mts_created IS NULL OR mts_created >= 0) AND "
            "(mts_updated IS NULL OR mts_updated >= 0) AND (mts_opening IS NULL OR "
            "mts_opening >= 0)",
            name="ck_ledger_observation_credit_amount",
        ),
    )


class LedgerObservationOfferHistoryRow(Base):
    __tablename__ = "ledger_observation_offer_history"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    venue_offer_id: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    amount_original: Mapped[Decimal | None] = mapped_column(Numeric)
    amount_remaining: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    rate: Mapped[Decimal | None] = mapped_column(Numeric)
    rate_observed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    period_days: Mapped[int | None] = mapped_column(Integer)
    offer_type: Mapped[str | None] = mapped_column(Text)
    flags: Mapped[dict[str, Any] | int | None] = mapped_column(_JSON)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    mts_created: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mts_updated: Mapped[int | None] = mapped_column(BigInteger)
    terminal_kind: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    raw: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "amount_remaining >= 0 AND (amount_original IS NULL OR amount_original >= 0) "
            "AND (rate IS NULL OR rate >= 0) AND (period_days IS NULL OR period_days > 0) "
            "AND mts_created >= 0 AND (mts_updated IS NULL OR mts_updated >= 0) AND "
            "occurred_at_ms >= 0",
            name="ck_ledger_offer_history_time",
        ),
    )


class LedgerObservationCreditHistoryRow(Base):
    __tablename__ = "ledger_observation_credit_history"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    venue_credit_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    rate: Mapped[Decimal | None] = mapped_column(Numeric)
    period_days: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    flags: Mapped[dict[str, Any] | int | None] = mapped_column(_JSON)
    mts_created: Mapped[int | None] = mapped_column(BigInteger)
    mts_updated: Mapped[int | None] = mapped_column(BigInteger)
    mts_opening: Mapped[int | None] = mapped_column(BigInteger)
    terminal_kind: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    raw: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    __table_args__ = (
        CheckConstraint("source_kind IN ('credit','loan')", name="ck_ledger_credit_history_source"),
        CheckConstraint(
            "amount >= 0 AND (rate IS NULL OR rate >= 0) AND (period_days IS NULL OR "
            "period_days > 0) AND (mts_created IS NULL OR mts_created >= 0) AND "
            "(mts_updated IS NULL OR mts_updated >= 0) AND (mts_opening IS NULL OR "
            "mts_opening >= 0) AND occurred_at_ms >= 0",
            name="ck_ledger_credit_history_time",
        ),
    )


class VenueOfferMirrorRow(Base):
    __tablename__ = "venue_offer_mirror"
    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, primary_key=True)
    venue_offer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    amount_original: Mapped[Decimal | None] = mapped_column(Numeric)
    amount_remaining: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    rate: Mapped[Decimal | None] = mapped_column(Numeric)
    rate_observed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    period_days: Mapped[int | None] = mapped_column(Integer)
    offer_type: Mapped[str | None] = mapped_column(Text)
    flags: Mapped[dict[str, Any] | int | None] = mapped_column(_JSON)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    mts_created: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mts_updated: Mapped[int | None] = mapped_column(BigInteger)
    last_accepted_observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    present_in_latest_accepted_snapshot: Mapped[bool] = mapped_column(Boolean, nullable=False)
    terminal_evidence_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation_offer_history.id", ondelete="RESTRICT"),
    )
    terminal_kind: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint(
            "amount_remaining >= 0 AND (amount_original IS NULL OR amount_original >= 0) "
            "AND (rate IS NULL OR rate >= 0) AND (period_days IS NULL OR period_days > 0) "
            "AND mts_created >= 0 AND (mts_updated IS NULL OR mts_updated >= 0)",
            name="ck_venue_offer_mirror_amount",
        ),
        CheckConstraint(
            "(terminal_evidence_id IS NULL) = (terminal_kind IS NULL) AND "
            "(terminal_evidence_id IS NULL OR NOT present_in_latest_accepted_snapshot)",
            name="ck_venue_offer_mirror_terminal",
        ),
        Index(
            "ix_venue_offer_mirror_live",
            "exchange_account_id",
            "deployment_environment",
            "symbol",
            postgresql_where=text("present_in_latest_accepted_snapshot"),
        ),
    )


class VenueCreditMirrorRow(Base):
    __tablename__ = "venue_credit_mirror"
    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, primary_key=True)
    venue_credit_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    rate: Mapped[Decimal | None] = mapped_column(Numeric)
    period_days: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    flags: Mapped[dict[str, Any] | int | None] = mapped_column(_JSON)
    mts_created: Mapped[int | None] = mapped_column(BigInteger)
    mts_updated: Mapped[int | None] = mapped_column(BigInteger)
    mts_opening: Mapped[int | None] = mapped_column(BigInteger)
    last_accepted_observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    present_in_latest_accepted_snapshot: Mapped[bool] = mapped_column(Boolean, nullable=False)
    terminal_evidence_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation_credit_history.id", ondelete="RESTRICT"),
    )
    terminal_kind: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("source_kind IN ('credit','loan')", name="ck_venue_credit_mirror_source"),
        CheckConstraint(
            "amount >= 0 AND (rate IS NULL OR rate >= 0) AND (period_days IS NULL OR "
            "period_days > 0) AND (mts_created IS NULL OR mts_created >= 0) AND "
            "(mts_updated IS NULL OR mts_updated >= 0) AND (mts_opening IS NULL OR "
            "mts_opening >= 0)",
            name="ck_venue_credit_mirror_amount",
        ),
        CheckConstraint(
            "(terminal_evidence_id IS NULL) = (terminal_kind IS NULL) AND "
            "(terminal_evidence_id IS NULL OR NOT present_in_latest_accepted_snapshot)",
            name="ck_venue_credit_mirror_terminal",
        ),
        Index(
            "ix_venue_credit_mirror_live",
            "exchange_account_id",
            "deployment_environment",
            "symbol",
            postgresql_where=text("present_in_latest_accepted_snapshot"),
        ),
    )


class SubmissionAttemptJournalRow(Base):
    __tablename__ = "submission_attempt_journal"
    attempt_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    execution_decision_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("execution_decisions.decision_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    exchange_account_id: Mapped[UUID] = _account()
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    basis_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "accepted_capital_basis.id", ondelete="RESTRICT", name="fk_submission_attempt_basis"
        ),
        nullable=False,
    )
    policy_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("capital_policy_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    authorization_evidence: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    seed_provenance: Mapped[dict[str, Any] | None] = mapped_column(_JSON)
    started_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "exchange_account_id",
            "deployment_environment",
            "attempt_seq",
            name="uq_submission_attempt_scope_seq",
        ),
        CheckConstraint(
            "attempt_seq >= 0 AND started_at_ms >= 0",
            name="ck_submission_attempt_nonnegative",
        ),
    )


class TransportOutcomeJournalRow(Base):
    __tablename__ = "transport_outcome_journal"
    attempt_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "submission_attempt_journal.attempt_id",
            ondelete="RESTRICT",
            name="fk_basis_attempt_attempt",
        ),
        primary_key=True,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    venue_offer_id: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    completed_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "kind IN ('ack','rejected','not_sent','unknown')", name="ck_transport_outcome_kind"
        ),
        CheckConstraint(
            "(kind = 'ack') = (venue_offer_id IS NOT NULL)", name="ck_transport_outcome_ack"
        ),
        CheckConstraint("completed_at_ms >= 0", name="ck_transport_outcome_time"),
    )


class QuarantineOpeningRow(Base):
    __tablename__ = "quarantine_opening"
    quarantine_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    exchange_account_id: Mapped[UUID] = _account()
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    intended_amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    opened_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    legacy_reconcile_event_seq: Mapped[int | None] = mapped_column(BigInteger)
    __table_args__ = (
        CheckConstraint(
            "intended_amount >= 0 AND opened_at_ms >= 0 AND (legacy_reconcile_event_seq "
            "IS NULL OR legacy_reconcile_event_seq >= 0)",
            name="ck_quarantine_opening_nonnegative",
        ),
    )


class QuarantineMemberRow(Base):
    __tablename__ = "quarantine_member"
    quarantine_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("quarantine_opening.quarantine_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    venue_offer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    amount_at_join: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    __table_args__ = (CheckConstraint("amount_at_join >= 0", name="ck_quarantine_member_amount"),)


class ExecutionResolutionJournalRow(Base):
    __tablename__ = "execution_resolution_journal"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    attempt_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("submission_attempt_journal.attempt_id", ondelete="RESTRICT"),
    )
    quarantine_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("quarantine_opening.quarantine_id", ondelete="RESTRICT")
    )
    exchange_account_id: Mapped[UUID] = _account()
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    venue_offer_id: Mapped[str | None] = mapped_column(Text)
    observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_kind: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    operator_request_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("uncertainty_resolution_requests.request_id", ondelete="RESTRICT"),
    )
    resolved_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    candidate_count: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "(attempt_id IS NULL) <> (quarantine_id IS NULL)",
            name="ck_execution_resolution_subject",
        ),
        CheckConstraint(
            "action IN ('bound_to_venue','not_accepted','manual')",
            name="ck_execution_resolution_action",
        ),
        CheckConstraint(
            "(action = 'bound_to_venue') = (venue_offer_id IS NOT NULL)",
            name="ck_execution_resolution_bound",
        ),
        CheckConstraint(
            "resolved_at_ms >= 0 AND (candidate_count IS NULL OR candidate_count >= 0)",
            name="ck_execution_resolution_time",
        ),
        Index(
            "uq_execution_resolution_attempt",
            "attempt_id",
            unique=True,
            postgresql_where=text("attempt_id IS NOT NULL"),
        ),
        Index(
            "uq_execution_resolution_quarantine",
            "quarantine_id",
            unique=True,
            postgresql_where=text("quarantine_id IS NOT NULL"),
        ),
    )


class AcceptedCapitalBasisRow(Base):
    __tablename__ = "accepted_capital_basis"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    exchange_account_id: Mapped[UUID] = _account()
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    observation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, unique=True)
    query_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, unique=True)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    start_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    accept_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    policy_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("capital_policy_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    authorization_block: Mapped[dict[str, Any] | None] = mapped_column(_JSON)
    credit_cells_present: Mapped[bool] = mapped_column(Boolean, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    accepted_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["observation_id", "accepted"],
            ["ledger_observation.id", "ledger_observation.accepted"],
            ondelete="RESTRICT",
            name="fk_accepted_basis_observation",
        ),
        CheckConstraint(
            "accepted AND start_revision >= 0 AND accept_revision = start_revision AND "
            "schema_version >= 1 AND accepted_at_ms >= 0",
            name="ck_accepted_basis_acceptance",
        ),
    )


class AcceptedCapitalBasisSymbolRow(Base):
    __tablename__ = "accepted_capital_basis_symbol"
    basis_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("accepted_capital_basis.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    available: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    offered: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    credits: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    unattributed_credits: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    foreign_offers: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "available >= 0 AND offered >= 0 AND credits >= 0 AND unattributed_credits >= "
            "0 AND foreign_offers >= 0 AND unattributed_credits <= credits",
            name="ck_accepted_basis_symbol_amount",
        ),
    )


class AcceptedCapitalBasisCellRow(Base):
    __tablename__ = "accepted_capital_basis_cell"
    basis_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("accepted_capital_basis.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    cell_id: Mapped[str] = mapped_column(Text, primary_key=True)
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    __table_args__ = (CheckConstraint("amount >= 0", name="ck_accepted_basis_cell_amount"),)


class AcceptedCapitalBasisAttemptRow(Base):
    __tablename__ = "accepted_capital_basis_attempt"
    basis_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("accepted_capital_basis.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    attempt_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("submission_attempt_journal.attempt_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    classification: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "classification IN ('reflected','settled','unresolved')",
            name="ck_accepted_basis_attempt_classification",
        ),
    )


class AcceptedCapitalBasisQuarantineRow(Base):
    __tablename__ = "accepted_capital_basis_quarantine"
    basis_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("accepted_capital_basis.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    quarantine_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("quarantine_opening.quarantine_id", ondelete="RESTRICT"),
        primary_key=True,
    )


LEDGER_TABLES = (
    CapitalCommandClockRow.__table__,
    LedgerObservationRow.__table__,
    LedgerObservationWalletRow.__table__,
    LedgerObservationOfferRow.__table__,
    LedgerObservationCreditRow.__table__,
    LedgerObservationOfferHistoryRow.__table__,
    LedgerObservationCreditHistoryRow.__table__,
    VenueOfferMirrorRow.__table__,
    VenueCreditMirrorRow.__table__,
    SubmissionAttemptJournalRow.__table__,
    TransportOutcomeJournalRow.__table__,
    QuarantineOpeningRow.__table__,
    QuarantineMemberRow.__table__,
    ExecutionResolutionJournalRow.__table__,
    AcceptedCapitalBasisRow.__table__,
    AcceptedCapitalBasisSymbolRow.__table__,
    AcceptedCapitalBasisCellRow.__table__,
    AcceptedCapitalBasisAttemptRow.__table__,
    AcceptedCapitalBasisQuarantineRow.__table__,
)
