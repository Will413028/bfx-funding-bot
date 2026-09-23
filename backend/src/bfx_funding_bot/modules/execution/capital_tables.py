"""Capital authority evidence, not a second reservation ledger.

Commitments remain immutable RESERVATION_INTENT events and submission attempts.
Policy history and accepted observations are append-only; only the policy pointer
is mutable. No migration seeds a policy or changes trading halt state.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON, Uuid

from bfx_funding_bot.core.db import Base

_JSON = JSON().with_variant(JSONB, "postgresql")


class CapitalPolicyRevisionRow(Base):
    __tablename__ = "capital_policy_revisions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    exchange_account_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("exchange_accounts.id", ondelete="RESTRICT"), nullable=False,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)

    __table_args__ = (UniqueConstraint(
        "exchange_account_id", "deployment_environment", "symbol", "revision",
        name="uq_capital_policy_revision_scope",
    ),)


class CapitalPolicyHeadRow(Base):
    __tablename__ = "capital_policy_heads"

    exchange_account_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("exchange_accounts.id", ondelete="RESTRICT"), primary_key=True,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    revision_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("capital_policy_revisions.id", ondelete="RESTRICT"), nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)


class CapitalQueryRow(Base):
    __tablename__ = "capital_snapshot_queries"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    exchange_account_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("exchange_accounts.id", ondelete="RESTRICT"), nullable=False,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    command_fence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    query_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (UniqueConstraint("exchange_account_id", "deployment_environment",
                                     "query_revision", name="uq_capital_query_revision"),)


class CapitalSnapshotRow(Base):
    __tablename__ = "capital_snapshots"

    event_seq: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("event_log.event_seq", ondelete="RESTRICT"), primary_key=True,
    )
    query_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("capital_snapshot_queries.id", ondelete="RESTRICT"), unique=True,
        nullable=False,
    )
    exchange_account_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("exchange_accounts.id", ondelete="RESTRICT"), nullable=False,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    command_fence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Fixed canonical classification derived from THIS event, never mutable buckets.
    classification: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    # event_log.prefix_hash of THIS snapshot's evidence event, recorded at
    # acceptance. Names the ledger prefix the classification was derived from, so a
    # read can check that claim against the evidence row it already loads. Nullable
    # only for rows accepted before this column existed; a read treats NULL as
    # unproven and blocks, and the next accepted snapshot supplies it.
    covered_prefix_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Why this snapshot cannot authorize, decided at acceptance. NULL means it
    # can. Recorded rather than raised because acceptance is how the system
    # records reality: refusing to record an observation because history is
    # unsettled would also remove the observations needed to settle it.
    authorization_blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
