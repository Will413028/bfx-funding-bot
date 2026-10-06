from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import JSON_DOCUMENT, Base

# Never a bare JSONB: it poisons Base.metadata.create_all on sqlite (global metadata,
# surfaces only in test ordering).
_JSON = JSON_DOCUMENT
# now() on Postgres, CURRENT_TIMESTAMP on sqlite. ANSI, works on both.
_NOW = func.current_timestamp()
# SQLite requires INTEGER (not BIGINT) for autoincrement PKs.
_BIG_PK = BigInteger().with_variant(Integer(), "sqlite")


class DiagnosticsRow(Base):
    """Forensic/audit records (DECISION / SAFETY_TRIGGER / CANCEL_AUDIT).

    Non-SoT, prunable (30-90d). Best-effort writes — a failure here NEVER
    blocks trading (spec §240-241). SoT lives in event_log, not here.

    occurred_at uses a real TIMESTAMPTZ datetime for forensic readability,
    intentionally NOT the epoch-millisecond BigInteger (*_ms) convention used
    by event_log (which needs ms precision for strict event sequencing).
    """

    __tablename__ = "diagnostics"

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    exchange_account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )

    __table_args__ = (
        Index("idx_diagnostics_acct_occurred", "exchange_account_id", "occurred_at"),
    )
