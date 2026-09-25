"""Safety-module persistence: nav_peak (guard calibration) + trading state (kill switch).

The two have OPPOSITE failure postures and must not be refactored into a shared
shape: losing a nav_peak only regresses the high-water mark (fail-permissive),
whereas an unreadable trading state must stop trading (fail-closed).

``trading_halt`` is the predecessor of ``trading_state``. Trading decisions no
longer read it; it remains only as the epoch the release ceremony binds canary
permits and release sessions to, and goes with that ceremony.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

# Register the referenced account table for metadata-only fixtures.
import bfx_funding_bot.modules.accounts.tables  # noqa: F401
from bfx_funding_bot.core.db import Base


class NavPeakRow(Base):
    __tablename__ = "nav_peak"

    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    exchange_account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    peak: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("exchange_account_id", "deployment_environment", "symbol"),
    )


class TradingHaltRow(Base):
    """Append-only halt/resume transitions. Current state = highest id for the realm.

    Append-only, not one mutable row, because the history IS the audit trail:
    "who resumed real-money trading, when, and on what grounds" is precisely the
    question that had no answer on 2026-07-27, and an overwritten row answers it
    even less. Rows are never updated or deleted.

    Ordering is by ``id``, never by ``created_at_ms``: the timestamp is supplied
    by the caller, and clock skew or a backfilled row must not be able to
    resurrect a superseded state on a safety control.
    """

    __tablename__ = "trading_halt"

    # sqlite only auto-assigns rowid for INTEGER PRIMARY KEY — a plain BigInteger
    # PK stays NULL there and every insert collides.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True,
    )
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    exchange_account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    halted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Why trading stopped, which decides how it may start again: `maintenance`
    # is an operator-requested pause and clears by an authenticated resume;
    # `safety` and `release` require the release-promotion path. Defaults to the
    # closed posture, never inferred from `reason`.
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="safety")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # Who made the transition (admin endpoint, bootstrap, an operator name).
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        Index(
            "ix_trading_halt_realm_id",
            "exchange_account_id", "deployment_environment", "id",
        ),
    )


class TradingStateRow(Base):
    """Append-only trading-state decisions. Current = highest id for the scope.

    ``state`` decides what the writer may do: ACTIVE trades, REDUCING only
    cancels, HALTED only cancels and has had the venue's funding offers
    cancelled. ``cause`` records who or what made the transition, and the
    allowed pairs are fixed by ``ck_trading_state_cause``.

    Ordering is by ``id``: PostgreSQL's insert trigger assigns it under a scope
    lock, validates the transition against the previous row and rejects every
    UPDATE/DELETE/TRUNCATE. SQLite fixtures get none of that; the repository
    validates transitions in code as well.
    """

    __tablename__ = "trading_state"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True,
    )
    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT", name="fk_trading_state_account"),
        nullable=False,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    cause: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    probation_multiplier: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    probation_started_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    legacy_halt_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        CheckConstraint("state IN ('ACTIVE', 'REDUCING', 'HALTED')", name="ck_trading_state_state"),
        CheckConstraint(
            "(state = 'ACTIVE' AND cause IN ('operator', 'auto')) OR "
            "(state = 'REDUCING' AND cause IN ('operator', 'material_deploy')) OR "
            "(state = 'HALTED' AND cause IN ('operator', 'kill_switch', 'auto'))",
            name="ck_trading_state_cause",
        ),
        CheckConstraint(
            "(probation_multiplier IS NULL AND probation_started_at_ms IS NULL) OR "
            "(state = 'ACTIVE' AND probation_multiplier > 0 AND probation_multiplier <= 1 "
            "AND probation_started_at_ms >= 0)",
            name="ck_trading_state_probation",
        ),
        CheckConstraint(
            "length(trim(actor)) > 0 AND length(trim(reason)) > 0 "
            "AND length(trim(deployment_environment)) > 0 AND created_at_ms >= 0",
            name="ck_trading_state_evidence",
        ),
        Index("ix_trading_state_scope_id", "exchange_account_id", "deployment_environment", "id"),
    )


class FundingCancelAllAuditRow(Base):
    """Append-only record of each venue funding cancel-all the kill switch issued.

    ``requested`` is written before the call; exactly one terminal row
    (``acknowledged`` / ``rejected`` / ``failed`` / ``skipped``) follows for the
    same ``attempt_id``. PostgreSQL rejects UPDATE/DELETE/TRUNCATE.
    """

    __tablename__ = "funding_cancel_all_audit"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True,
    )
    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT",
                   name="fk_funding_cancel_all_audit_account"),
        nullable=False,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    trading_state_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("trading_state.id", ondelete="RESTRICT",
                   name="fk_funding_cancel_all_audit_trading_state"),
        nullable=False,
    )
    attempt_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    venue_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "phase IN ('requested', 'acknowledged', 'rejected', 'failed', 'skipped')",
            name="ck_funding_cancel_all_audit_phase",
        ),
        CheckConstraint(
            "(phase = 'requested' AND venue_status IS NULL AND detail IS NULL) OR phase <> 'requested'",
            name="ck_funding_cancel_all_audit_request_shape",
        ),
        CheckConstraint(
            "length(currency) BETWEEN 1 AND 16 AND length(trim(actor)) > 0 "
            "AND (detail IS NULL OR length(detail) <= 512) "
            "AND (venue_status IS NULL OR length(venue_status) <= 64) AND occurred_at_ms >= 0",
            name="ck_funding_cancel_all_audit_evidence",
        ),
        Index("ix_funding_cancel_all_audit_scope_id",
              "exchange_account_id", "deployment_environment", "id"),
        Index("uq_funding_cancel_all_audit_request", "attempt_id", unique=True,
              postgresql_where=text("phase = 'requested'"),
              sqlite_where=text("phase = 'requested'")),
        Index("uq_funding_cancel_all_audit_outcome", "attempt_id", unique=True,
              postgresql_where=text("phase <> 'requested'"),
              sqlite_where=text("phase <> 'requested'")),
    )


class NavWindowSampleRow(Base):
    """Loss-limiter 24h window samples, so a restart does not forget a recent loss (T9).

    ReconcileNavTracker computes realized_loss_pct_24h from the highest NAV seen
    in the last 24h. Kept in memory only, a restart right after a loss rebuilt the
    window from the post-loss NAV and the limiter read zero. Rows are written only
    when NAV changes (or every few minutes), read back at boot, and pruned after
    two days. Fail-permissive like nav_peak: the reconcile path never waits on it.
    """

    __tablename__ = "nav_window_samples"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True,
    )
    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("exchange_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    nav: Mapped[Decimal] = mapped_column(Numeric, nullable=False)

    __table_args__ = (
        Index("ix_nav_window_samples_scope_symbol_time",
              "exchange_account_id", "deployment_environment", "symbol", "occurred_at_ms"),
    )
