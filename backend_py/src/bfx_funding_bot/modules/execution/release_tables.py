"""Session-specific control handoff; runtime transition columns are worker-only."""
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, BigInteger, CheckConstraint, ForeignKey, Integer, Numeric, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base

_JSON = JSON().with_variant(JSONB, "postgresql")


class ReleaseSessionRow(Base):
    __tablename__ = "release_sessions"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    exchange_account_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("exchange_accounts.id"))
    deployment_environment: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text)
    cell: Mapped[str] = mapped_column(Text)
    strategy: Mapped[str] = mapped_column(Text)
    max_amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger)
    created_at_ms: Mapped[int] = mapped_column(BigInteger)
    requested_by: Mapped[str] = mapped_column(Text)
    requested_action: Mapped[str] = mapped_column(Text, server_default="prepare")
    request_revision: Mapped[int] = mapped_column(Integer, server_default="1")
    # Webapi may only insert/update the request fields above, never these.
    processed_revision: Mapped[int] = mapped_column(Integer, server_default="0")
    state: Mapped[str] = mapped_column(Text, server_default="requested")
    binding: Mapped[dict[str, Any] | None] = mapped_column(_JSON)
    halt_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("trading_halt.id"))
    minimum_amount: Mapped[Decimal | None] = mapped_column(Numeric)
    authorized_by: Mapped[str | None] = mapped_column(Text)
    authorized_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    consumed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    permit_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("canary_command_permits.permit_id"))
    decision_id: Mapped[str | None] = mapped_column(Text, ForeignKey("execution_decisions.decision_id"))
    attempt_id: Mapped[UUID | None] = mapped_column(Uuid)
    exact_amount: Mapped[Decimal | None] = mapped_column(Numeric)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(_JSON)
    promoted_halt_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("trading_halt.id"))
    reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("state IN ('requested','prepared','authorized','consumed','observed','validated','promoted','blocked')", name="ck_release_state"),
        CheckConstraint("requested_action IN ('prepare','authorize','validate','promote')", name="ck_release_action"),
        CheckConstraint("symbol = 'fUST' AND max_amount > 0 AND expires_at_ms > created_at_ms", name="ck_release_scope"),
        CheckConstraint("CAST(max_amount AS TEXT) NOT IN ('NaN','Infinity','-Infinity')", name="ck_release_finite"),
        CheckConstraint("state IN ('requested','blocked') OR (binding IS NOT NULL AND halt_id IS NOT NULL AND minimum_amount IS NOT NULL AND minimum_amount > 0 AND minimum_amount <= max_amount)", name="ck_release_prepared"),
        CheckConstraint("state IN ('requested','prepared','blocked') OR (authorized_by IS NOT NULL AND authorized_at_ms IS NOT NULL)", name="ck_release_authorized"),
        CheckConstraint("state NOT IN ('consumed','observed','validated','promoted') OR (permit_id IS NOT NULL AND decision_id IS NOT NULL AND attempt_id IS NOT NULL AND consumed_at_ms IS NOT NULL AND exact_amount IS NOT NULL)", name="ck_release_consumed"),
        CheckConstraint("state NOT IN ('validated','promoted') OR evidence IS NOT NULL", name="ck_release_validated"),
        CheckConstraint("state <> 'promoted' OR promoted_halt_id IS NOT NULL", name="ck_release_promoted"),
        CheckConstraint("exact_amount IS NULL OR (exact_amount > 0 AND exact_amount <= max_amount)", name="ck_release_amount"),
        CheckConstraint("request_revision >= processed_revision AND processed_revision >= 0", name="ck_release_revision"),
    )


class ReleaseAuditRow(Base):
    __tablename__ = "release_session_audit"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    session_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("release_sessions.id"))
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger)
    evidence: Mapped[dict[str, Any]] = mapped_column(_JSON)
