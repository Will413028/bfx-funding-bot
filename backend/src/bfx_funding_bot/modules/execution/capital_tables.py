"""Capital authority evidence, not a second reservation ledger.

Commitments remain immutable RESERVATION_INTENT events and submission attempts.
Policy history and accepted observations are append-only; only the policy pointer
is mutable. No migration seeds a policy or changes trading halt state.

``capital_policy_requests`` is the operator-request outbox for the per-currency
``enabled`` flag (``execution.operator_requests``): the web API inserts a
request, the account daemon appends the policy revision.
"""
from __future__ import annotations

from typing import Any, ClassVar, Literal, get_args
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
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


PolicyRequestAction = Literal["enable", "disable"]
POLICY_REQUEST_ACTIONS: tuple[str, ...] = get_args(PolicyRequestAction)


class CapitalPolicyRequestRow(Base):
    """An operator's request to enable or disable one currency's applied policy.

    The web API inserts only the request columns; the account daemon appends
    the new policy revision and writes one outcome (``applied`` names the
    revision in force afterwards, also when nothing had to change). A separate
    outbox from ``trading_control_requests``: its subject is one currency's
    policy, not the account's trading state, so its pending slots are per
    currency and a kill never shares a queue with it.
    """

    __tablename__ = "capital_policy_requests"
    REQUEST_COLUMNS: ClassVar[tuple[str, ...]] = (
        "request_id", "exchange_account_id", "deployment_environment", "symbol", "action",
        "reason", "requested_by", "created_at_ms",
    )
    WORKER_COLUMNS: ClassVar[tuple[str, ...]] = (
        "state", "processed_at_ms", "outcome_reason", "policy_revision_id",
    )

    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT",
                   name="fk_capital_policy_requests_account"),
        nullable=False,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'requested'"))
    processed_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    outcome_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_revision_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("capital_policy_revisions.id", ondelete="RESTRICT",
                   name="fk_capital_policy_requests_revision"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint("action IN ('enable', 'disable')", name="ck_capital_policy_requests_action"),
        CheckConstraint(
            "length(symbol) BETWEEN 3 AND 16 AND length(trim(reason)) BETWEEN 1 AND 500 "
            "AND length(trim(requested_by)) > 0 AND created_at_ms >= 0",
            name="ck_capital_policy_requests_evidence",
        ),
        CheckConstraint(
            "(state = 'requested' AND processed_at_ms IS NULL AND outcome_reason IS NULL "
            "AND policy_revision_id IS NULL) OR "
            "(state = 'applied' AND processed_at_ms IS NOT NULL AND policy_revision_id IS NOT NULL) OR "
            "(state IN ('rejected', 'failed') AND processed_at_ms IS NOT NULL "
            "AND outcome_reason IS NOT NULL AND policy_revision_id IS NULL)",
            name="ck_capital_policy_requests_outcome",
        ),
        # One waiting request per currency and action: a pending enable never
        # makes a disable wait for a 409, and the worker applies them in order.
        Index("uq_capital_policy_requests_pending", "exchange_account_id",
              "deployment_environment", "symbol", "action", unique=True,
              postgresql_where=text("state = 'requested'"),
              sqlite_where=text("state = 'requested'")),
        Index("ix_capital_policy_requests_queue", "exchange_account_id",
              "deployment_environment", "state", "created_at_ms"),
    )
