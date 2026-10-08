"""``capital_policy_requests``: the operator-request outbox for the per-currency ``enabled`` flag
(``execution.operator_requests``). The web API inserts a request, the account daemon appends the
policy revision.

The legacy capital evidence tables (``capital_snapshot_queries``, ``capital_snapshots``) live
frozen in ``legacy_archive``; migrations own them and no metadata describes them.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar, Literal, get_args
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from bfx_funding_bot.core.db import Base

PolicyRequestAction = Literal["enable", "disable"]
POLICY_REQUEST_ACTIONS: tuple[str, ...] = get_args(PolicyRequestAction)


class CapitalPolicyRequestRow(Base):
    """An operator's request to enable or disable one currency's applied policy.

    The web API inserts only the request columns; the account daemon appends
    the new policy revision, which names the request, and writes one outcome
    (``applied``, also when nothing had to change: its reason says so). A separate
    outbox from ``trading_control_requests``: its subject is one currency's
    policy, not the account's trading state, so its pending slots are per
    currency and a kill never shares a queue with it.
    """

    __tablename__ = "capital_policy_requests"
    REQUEST_COLUMNS: ClassVar[tuple[str, ...]] = (
        "request_id", "exchange_account_id", "deployment_environment", "symbol", "action",
        "reason", "requested_by", "created_at_ms",
    )
    WORKER_COLUMNS: ClassVar[tuple[str, ...]] = ("state", "processed_at_ms", "outcome_reason")
    # Closed product column: in the table, not mapped (appended below the class). The revision
    # an applied request wrote names the request instead
    # (``capital_policy_revisions.operator_request_id``, 5e820d6dc7da); no role may write this
    # one. It stays one release for the previous image's web API reads; the next drops it.
    CLOSED_COLUMNS: ClassVar[tuple[str, ...]] = ("policy_revision_id",)

    @classmethod
    def pending_index(cls, values: Mapping[str, object]) -> str:
        """The partial unique index a new request takes its pending slot in (``insert_request``)."""
        return "uq_capital_policy_requests_pending"

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

    __table_args__ = (
        CheckConstraint("action IN ('enable', 'disable')", name="ck_capital_policy_requests_action"),
        CheckConstraint(
            "length(symbol) BETWEEN 3 AND 16 AND length(trim(reason)) BETWEEN 1 AND 500 "
            "AND length(trim(requested_by)) > 0 AND created_at_ms >= 0",
            name="ck_capital_policy_requests_evidence",
        ),
        CheckConstraint(
            "(state = 'requested' AND processed_at_ms IS NULL AND outcome_reason IS NULL) OR "
            "(state = 'applied' AND processed_at_ms IS NOT NULL) OR "
            "(state IN ('rejected', 'failed') AND processed_at_ms IS NOT NULL "
            "AND outcome_reason IS NOT NULL)",
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
        # What a revision's composite foreign key references.
        UniqueConstraint("request_id", "exchange_account_id", "deployment_environment", "symbol",
                         name="uq_capital_policy_requests_scope"),
    )


_REQUESTS_TABLE = CapitalPolicyRequestRow.__table__
assert isinstance(_REQUESTS_TABLE, Table)
_REQUESTS_TABLE.append_column(Column(
    "policy_revision_id", Uuid,
    ForeignKey("capital_policy_revisions.id", ondelete="RESTRICT",
               name="fk_capital_policy_requests_revision"),
    nullable=True,
))
