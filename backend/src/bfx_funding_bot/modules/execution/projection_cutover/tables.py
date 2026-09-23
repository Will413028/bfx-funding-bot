"""Separate PostgreSQL archive metadata: never part of runtime create_all.

The migration principal owns this schema and must be a dedicated operator.
Triggers/ACLs are migration-owned; metadata alone does not install the contract.
Receipt byte fields use the versioned codec, allowing downstream evidence to
retain UUID, Decimal and timestamp identity without a lossy JSON conversion.
"""

from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, LargeBinary, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ArchiveBase(DeclarativeBase):
    pass


class ArchiveRun(ArchiveBase):
    __tablename__ = "runs"
    __table_args__ = {"schema": "projection_audit"}  # noqa: RUF012 - SQLAlchemy declarative contract

    run_id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True)
    exchange_account_id: Mapped[UUID] = mapped_column(PG_UUID, nullable=False)
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    manifest: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class ArchiveRow(ArchiveBase):
    __tablename__ = "rows"
    __table_args__ = {"schema": "projection_audit"}  # noqa: RUF012 - SQLAlchemy declarative contract

    run_id: Mapped[UUID] = mapped_column(
        PG_UUID,
        ForeignKey("projection_audit.runs.run_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    table_name: Mapped[str] = mapped_column(Text, primary_key=True)
    row_key: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    encoded_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    row_digest: Mapped[str] = mapped_column(Text, nullable=False)


class ArchiveReceipt(ArchiveBase):
    __tablename__ = "receipts"
    __table_args__ = {"schema": "projection_audit"}  # noqa: RUF012 - SQLAlchemy declarative contract

    run_id: Mapped[UUID] = mapped_column(
        PG_UUID,
        ForeignKey("projection_audit.runs.run_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    snapshot_identity: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    new_stream: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    active_hashes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    evidence: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
