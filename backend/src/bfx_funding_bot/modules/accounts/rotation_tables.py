"""Credential rotation through the operator-request outbox (``execution.operator_requests``).

``credential_rotation_recipients``: the public key each bot run publishes once it is the
writer (``execution.credential_rotation``); the private key exists only in that process.
``credential_rotation_requests``: the operator CLI inserts the request columns -- the new
key sealed to the current run's recipient (``core.sealed_credential``) and the active
credential it replaces -- and the account daemon verifies the key, swaps the active
credential in one transaction and records one outcome.

Both tables are append-only for the runtime roles: recipients are never changed, and a
request leaves ``requested`` exactly once (the migration's triggers).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    LargeBinary,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base

LABEL_MAX = 64


class CredentialRotationRecipientRow(Base):
    """One bot run's HPKE public key: what the CLI seals a new credential to."""

    __tablename__ = "credential_rotation_recipients"

    recipient_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT",
                   name="fk_credential_rotation_recipients_account"),
        nullable=False,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("bot_runs.run_id", ondelete="RESTRICT",
                   name="fk_credential_rotation_recipients_run"),
        nullable=False,
    )
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint("octet_length(public_key) = 32 AND created_at_ms >= 0",
                        name="ck_credential_rotation_recipients_key").ddl_if(dialect="postgresql"),
        Index("ix_credential_rotation_recipients_run", "exchange_account_id",
              "deployment_environment", "run_id", "created_at_ms"),
    )


class CredentialRotationRequestRow(Base):
    """An operator's request to replace the account's active Bitfinex credential.

    The CLI inserts only the request columns; ``sealed_credential`` opens only in the bot
    run whose recipient it names. The daemon writes one outcome; the credential an applied
    rotation made active names the request (``exchange_account_credentials.operator_request_id``),
    as every outbox's effect does.
    """

    __tablename__ = "credential_rotation_requests"
    REQUEST_COLUMNS: ClassVar[tuple[str, ...]] = (
        "request_id", "exchange_account_id", "deployment_environment", "recipient_id",
        "replaces_credential_id", "sealed_credential", "label", "reason", "requested_by",
        "created_at_ms",
    )
    WORKER_COLUMNS: ClassVar[tuple[str, ...]] = ("state", "processed_at_ms", "outcome_reason")

    @classmethod
    def pending_index(cls, values: Mapping[str, object]) -> str:
        """The partial unique index a new request takes its pending slot in (``insert_request``)."""
        return "uq_credential_rotation_requests_pending"

    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT",
                   name="fk_credential_rotation_requests_account"),
        nullable=False,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("credential_rotation_recipients.recipient_id", ondelete="RESTRICT",
                   name="fk_credential_rotation_requests_recipient"),
        nullable=False,
    )
    replaces_credential_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_account_credentials.id", ondelete="RESTRICT",
                   name="fk_credential_rotation_requests_replaces"),
        nullable=False,
    )
    sealed_credential: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'requested'"))
    processed_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    outcome_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "octet_length(sealed_credential) BETWEEN 64 AND 1024",
            name="ck_credential_rotation_requests_sealed",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "length(trim(reason)) BETWEEN 1 AND 500 AND length(label) <= 64 "
            "AND length(trim(requested_by)) > 0 AND created_at_ms >= 0",
            name="ck_credential_rotation_requests_evidence",
        ),
        CheckConstraint(
            "(state = 'requested' AND processed_at_ms IS NULL AND outcome_reason IS NULL) OR "
            "(state = 'applied' AND processed_at_ms IS NOT NULL) OR "
            "(state IN ('rejected', 'failed') AND processed_at_ms IS NOT NULL "
            "AND outcome_reason IS NOT NULL)",
            name="ck_credential_rotation_requests_outcome",
        ),
        # One waiting rotation per account and environment.
        Index("uq_credential_rotation_requests_pending", "exchange_account_id",
              "deployment_environment", unique=True,
              postgresql_where=text("state = 'requested'"),
              sqlite_where=text("state = 'requested'")),
        Index("ix_credential_rotation_requests_queue", "exchange_account_id",
              "deployment_environment", "state", "created_at_ms"),
    )


__all__ = [
    "LABEL_MAX",
    "CredentialRotationRecipientRow",
    "CredentialRotationRequestRow",
]
