"""``uncertainty_resolution_requests``: the operator's adjudication outbox for the account daemon.

The legacy submit-attempt and uncertainty projections (``submission_attempts``,
``execution_uncertainties``) live frozen in ``legacy_archive``; migrations own them and no
metadata describes them.
"""
from __future__ import annotations

from typing import ClassVar, Literal, get_args
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.operator_requests import REQUEST_STATES

_UUID = PG_UUID(as_uuid=True).with_variant(Uuid(as_uuid=True), "sqlite")


# Single source for the action set: the Literal types the code, the tuple
# builds the CHECK below, and a test pins the migration to both.
ResolutionAction = Literal["bind_to_venue", "mark_not_accepted", "manual_resolution"]
UNCERTAINTY_RESOLUTION_ACTIONS: tuple[str, ...] = get_args(ResolutionAction)


class UncertaintyResolutionRequestRow(Base):
    """Operator adjudication handed to the account daemon (ADR D4').

    Not a projection: the web API only inserts the request columns and the
    daemon's resolution worker alone appends the resolution event, so the web
    API needs no ledger or projection write privilege. ``uncertainty_id`` is
    deliberately not a foreign key: a ledger uncertainty is either a submission
    attempt or a quarantine (``operator_reads.get_uncertainty``), two tables.
    """

    __tablename__ = "uncertainty_resolution_requests"
    # The operator-request column split (operator_requests): the web API's
    # column-scoped INSERT grant, and the daemon's UPDATE grant.
    REQUEST_COLUMNS: ClassVar[tuple[str, ...]] = (
        "request_id", "exchange_account_id", "deployment_environment", "uncertainty_id",
        "action", "observation_id", "venue_offer_id", "decision", "reason", "requested_by",
        "created_at_ms",
    )
    WORKER_COLUMNS: ClassVar[tuple[str, ...]] = (
        "state", "processed_at_ms", "outcome_reason",
    )
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
    # The ledger observation the operator cited.
    observation_id: Mapped[UUID] = mapped_column(
        _UUID,
        ForeignKey(
            "ledger_observation.id",
            ondelete="RESTRICT",
            name="fk_uncertainty_resolution_requests_observation",
        ),
        nullable=False,
    )
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
    outcome_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"action IN ({', '.join(repr(a) for a in UNCERTAINTY_RESOLUTION_ACTIONS)})",
            name="ck_uncertainty_resolution_requests_action",
        ),
        CheckConstraint(
            f"state IN ({', '.join(repr(s) for s in REQUEST_STATES)})",
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
            "(state = 'requested' AND processed_at_ms IS NULL AND outcome_reason IS NULL) OR "
            "(state = 'applied' AND processed_at_ms IS NOT NULL AND outcome_reason IS NULL) OR "
            "(state IN ('rejected', 'failed') AND processed_at_ms IS NOT NULL "
            "AND outcome_reason IS NOT NULL)",
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

