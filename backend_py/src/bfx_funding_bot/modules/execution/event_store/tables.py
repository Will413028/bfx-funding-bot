from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base

# JSONB on Postgres, generic JSON on sqlite (unit tests).
_JSON = JSON().with_variant(JSONB, "postgresql")

# now() on Postgres, CURRENT_TIMESTAMP on sqlite (unit tests).
# func.current_timestamp() is ANSI SQL and works on both dialects.
_NOW = func.current_timestamp()

# SQLite requires INTEGER (not BIGINT) for autoincrement PKs.
_BIG_PK = BigInteger().with_variant(Integer(), "sqlite")


class EventLogRow(Base):
    """Append-only domain-event log. SoT. No UPDATE/DELETE."""

    __tablename__ = "event_log"

    event_seq: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    exchange_account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    cid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    venue_offer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    venue_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )

    __table_args__ = (
        Index("idx_event_log_acct_env_seq", "account_id", "deployment_environment", "event_seq"),
        Index("idx_event_log_cid", "cid"),
        Index("idx_event_log_voi", "venue_offer_id"),
        Index(
            "uq_event_log_dedup",
            "account_id", "deployment_environment", "event_type", "venue_offer_id", "venue_seq",
            unique=True,
        ),
    )


class OfferClaimRow(Base):
    """Snapshot: offer FSM projection, cid-keyed (composite PK with tenant scope)."""

    __tablename__ = "offer_claims"

    cid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    exchange_account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    venue_offer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'fUST'"))
    size_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    signal_correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Nullable only for pre-Task-4 historical rows. New submit paths always
    # project the immutable reservation reference's audited decision id.
    execution_decision_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_updated_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("account_id", "deployment_environment", "cid"),
        Index("idx_offer_claims_voi", "venue_offer_id"),
        Index(
            "uq_offer_claims_venue_offer_id",
            "account_id", "deployment_environment", "venue_offer_id",
            unique=True,
            postgresql_where=text("venue_offer_id IS NOT NULL"),
            sqlite_where=text("venue_offer_id IS NOT NULL"),
        ),
        Index(
            "uq_offer_claims_execution_decision_id",
            "account_id", "deployment_environment", "execution_decision_id",
            unique=True,
            postgresql_where=text("execution_decision_id IS NOT NULL"),
            sqlite_where=text("execution_decision_id IS NOT NULL"),
        ),
    )


class PositionStateRow(Base):
    """Snapshot: ledger projection. One row per (account, env, symbol).

    Native units per symbol: `reserved`/`realized` are in the symbol's own
    currency (fUST -> USDT), never summed across symbols. The symbol column +
    composite PK let multiple funding currencies coexist for one tenant.
    """

    __tablename__ = "position_state"

    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    exchange_account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    reserved: Mapped[Decimal] = mapped_column(Numeric, nullable=False, server_default=text("0"))
    realized: Mapped[Decimal] = mapped_column(Numeric, nullable=False, server_default=text("0"))
    # Event/domain time of the latest projected event (epoch ms, from the event's
    # occurred_at_ms) — mirrors offer_claims.last_updated_ms. NOT wall-clock: a
    # projection is a deterministic function of the event stream, so rebuild
    # reproduces it exactly. Projector freshness/lag is monitored via
    # last_event_seq vs the event_log head, not a wall-clock timestamp.
    last_updated_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    last_reconciled_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    n_credits: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("account_id", "deployment_environment", "symbol"),
    )


class ReconcileObservationRow(Base):
    """Append-only checkpoint: one row per reconcile tick. Immutable audit of
    venue truth at observation time + the event_log fence it was taken at.
    position_state = latest ReconcileObservationRow ⊕ domain events with
    event_seq > event_seq_fence (see store.rebuild_snapshot_from_log)."""

    __tablename__ = "reconcile_observation"

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    exchange_account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'fUST'"))
    reserved_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    realized_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    n_offers: Mapped[int] = mapped_column(Integer, nullable=False)
    n_credits: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_seq_fence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )

    __table_args__ = (
        Index("idx_reconcile_obs_acct_env_symbol_id",
              "account_id", "deployment_environment", "symbol", "id"),
    )
