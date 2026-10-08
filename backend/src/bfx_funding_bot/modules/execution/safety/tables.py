"""Safety-module persistence: nav_peak (guard calibration) + trading state (kill switch).

The two have OPPOSITE failure postures and must not be refactored into a shared
shape: losing a nav_peak only regresses the high-water mark (fail-permissive),
whereas an unreadable trading state must stop trading (fail-closed).

``trading_halt``, the predecessor of ``trading_state``, was archived with the
release ceremony it served (``release_archive`` schema, migration c74d45a54e46);
``trading_state.legacy_halt_id`` still names the row a seeded state came from.
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import ClassVar
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
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


_DIGEST_RE = "'^sha256:[0-9a-f]{64}$'"


class TradingStateRow(Base):
    """Append-only trading-state decisions. Current = highest id for the scope.

    ``state`` decides what the writer may do: ACTIVE trades, HALTED only
    cancels and has had venue funding offers cancelled. ``cause`` records who
    or what made the transition (``operator`` or ``auto``). Rows written before
    revision 5b1e7c9d2a40 may still say REDUCING or ``material_deploy``: the
    CHECKs are NOT VALID so that history stays as it was recorded.

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
    legacy_halt_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The operator request this row applies; NULL for an automatic halt or resume and for
    # /admin/halt. The foreign key carries the scope, so it names a request of this scope.
    operator_request_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)

    __table_args__ = (
        CheckConstraint("state IN ('ACTIVE', 'HALTED')", name="ck_trading_state_state"),
        CheckConstraint("cause IN ('operator', 'auto')", name="ck_trading_state_cause"),
        CheckConstraint(
            "length(trim(actor)) > 0 AND length(trim(reason)) > 0 "
            "AND length(trim(deployment_environment)) > 0 AND created_at_ms >= 0",
            name="ck_trading_state_evidence",
        ),
        Index("ix_trading_state_scope_id", "exchange_account_id", "deployment_environment", "id"),
        # What the cancel-all audit's composite foreign key references.
        UniqueConstraint("id", "exchange_account_id", "deployment_environment",
                         name="uq_trading_state_scope"),
        ForeignKeyConstraint(
            ["operator_request_id", "exchange_account_id", "deployment_environment"],
            ["trading_control_requests.request_id", "trading_control_requests.exchange_account_id",
             "trading_control_requests.deployment_environment"],
            ondelete="RESTRICT", name="fk_trading_state_operator_request"),
        # A request is applied by at most one row.
        Index("uq_trading_state_operator_request", "operator_request_id", unique=True,
              postgresql_where=text("operator_request_id IS NOT NULL"),
              sqlite_where=text("operator_request_id IS NOT NULL")),
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
    # The HALTED decision this cancel-all enforces, in the same scope.
    trading_state_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    venue_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # The kill request whose cancel-all this is; NULL for /admin/halt and automatic halts.
    # A re-sent kill writes no new trading state, so this is its only link to the venue call.
    operator_request_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)

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
        ForeignKeyConstraint(
            ["trading_state_id", "exchange_account_id", "deployment_environment"],
            ["trading_state.id", "trading_state.exchange_account_id",
             "trading_state.deployment_environment"],
            ondelete="RESTRICT", name="fk_funding_cancel_all_audit_trading_state"),
        ForeignKeyConstraint(
            ["operator_request_id", "exchange_account_id", "deployment_environment"],
            ["trading_control_requests.request_id", "trading_control_requests.exchange_account_id",
             "trading_control_requests.deployment_environment"],
            ondelete="RESTRICT", name="fk_funding_cancel_all_audit_operator_request"),
    )


class TradingControlRequestRow(Base):
    """An operator's resume or kill request, applied by the account daemon.

    The web API inserts only the request columns; the daemon writes one outcome
    (``applied`` / ``rejected`` / ``failed``) and nothing rewrites it
    (the operator-request contract, ``execution.operator_requests``).
    """

    __tablename__ = "trading_control_requests"
    REQUEST_COLUMNS: ClassVar[tuple[str, ...]] = (
        "request_id", "exchange_account_id", "deployment_environment", "action",
        "reason", "requested_by", "created_at_ms",
    )
    WORKER_COLUMNS: ClassVar[tuple[str, ...]] = ("state", "processed_at_ms", "outcome_reason")
    # Closed product column: in the table, not mapped (appended below the class, after the
    # mapper has taken its columns). The trading state an applied request wrote names the
    # request instead (``trading_state.operator_request_id``, 5e820d6dc7da); no role may write
    # this one. It stays one release because the image before this one maps it, and its web
    # API's reads of this model name every mapped column while a deploy migrates; the next
    # release drops it.
    CLOSED_COLUMNS: ClassVar[tuple[str, ...]] = ("trading_state_id",)

    @classmethod
    def pending_index(cls, values: Mapping[str, object]) -> str:
        """The partial unique index a new request takes its pending slot in (``insert_request``)."""
        if values["action"] == "kill":
            return "uq_trading_control_requests_pending_kill"
        return "uq_trading_control_requests_pending"

    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT",
                   name="fk_trading_control_requests_account"),
        nullable=False,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'requested'"))
    processed_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    outcome_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("action IN ('resume', 'kill')", name="ck_trading_control_requests_action"),
        CheckConstraint(
            "length(trim(reason)) BETWEEN 1 AND 500 AND length(trim(requested_by)) > 0 "
            "AND created_at_ms >= 0",
            name="ck_trading_control_requests_evidence",
        ),
        CheckConstraint(
            "(state = 'requested' AND processed_at_ms IS NULL AND outcome_reason IS NULL) OR "
            "(state = 'applied' AND processed_at_ms IS NOT NULL) OR "
            "(state IN ('rejected', 'failed') AND processed_at_ms IS NOT NULL "
            "AND outcome_reason IS NOT NULL)",
            name="ck_trading_control_requests_outcome",
        ),
        # One waiting request per scope, plus one kill beside it: a pending
        # resume never makes a kill wait for a 409.
        Index("uq_trading_control_requests_pending", "exchange_account_id",
              "deployment_environment", unique=True,
              postgresql_where=text("state = 'requested' AND action <> 'kill'"),
              sqlite_where=text("state = 'requested' AND action <> 'kill'")),
        Index("uq_trading_control_requests_pending_kill", "exchange_account_id",
              "deployment_environment", unique=True,
              postgresql_where=text("state = 'requested' AND action = 'kill'"),
              sqlite_where=text("state = 'requested' AND action = 'kill'")),
        Index("ix_trading_control_requests_queue", "exchange_account_id",
              "deployment_environment", "state", "created_at_ms"),
        # What an effect's composite foreign key references.
        UniqueConstraint("request_id", "exchange_account_id", "deployment_environment",
                         name="uq_trading_control_requests_scope"),
    )



_REQUESTS_TABLE = TradingControlRequestRow.__table__
assert isinstance(_REQUESTS_TABLE, Table)
_REQUESTS_TABLE.append_column(Column(
    "trading_state_id", BigInteger,
    ForeignKey("trading_state.id", ondelete="RESTRICT",
               name="fk_trading_control_requests_trading_state"),
    nullable=True,
))

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
