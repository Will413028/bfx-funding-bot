from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
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


class EventPrefixHashRow(Base):
    """Rolling hash of the event prefix ending at ``event_seq``.

    Kept beside the ledger rather than on it. ``event_log`` is append-only and a
    database trigger enforces that for capital-bearing rows, so a chain written as
    an UPDATE after INSERT would be refused for exactly the events capital reads
    depend on -- and historical rows could never be sealed at all. Insert-only here
    keeps the ledger immutable and the chain complete.

    Verification stays one indexed row: a projection names the prefix it covers,
    and the check reads that prefix's hash by ``event_seq``.
    """

    __tablename__ = "event_prefix_hashes"

    event_seq: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("event_log.event_seq", ondelete="RESTRICT"), primary_key=True,
    )
    exchange_account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    prefix_hash: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index(
            "idx_event_prefix_scope_seq",
            "exchange_account_id", "deployment_environment", "event_seq",
        ),
    )


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
    # Native schema-v3 identity.  Historical rows remain nullable and receive
    # a deterministic UUIDv5 at replay time without rewriting the append-only
    # event payload or sequence.
    event_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("2")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )


    __table_args__ = (
        Index(
            "idx_event_log_acct_env_seq",
            "exchange_account_id", "deployment_environment", "event_seq",
        ),
        Index("idx_event_log_cid", "cid"),
        Index("idx_event_log_voi", "venue_offer_id"),
        Index(
            "uq_event_log_event_id",
            "exchange_account_id", "deployment_environment", "event_id",
            unique=True,
            postgresql_where=text("event_id IS NOT NULL"),
            sqlite_where=text("event_id IS NOT NULL"),
        ),
        # Transitional SQLite/legacy-realm rows have no UUID owner yet.  This
        # narrow companion index still prevents the same legacy account/env
        # from accepting two native event IDs; production writers reject such
        # rows before they reach the canonical UUID index above.
        Index(
            "uq_event_log_legacy_event_id",
            "account_id", "deployment_environment", "event_id",
            unique=True,
            postgresql_where=text(
                "event_id IS NOT NULL AND exchange_account_id IS NULL"
            ),
            sqlite_where=text(
                "event_id IS NOT NULL AND exchange_account_id IS NULL"
            ),
            info={"identity_legacy_fixture": True},
        ),
        Index(
            "uq_event_log_dedup",
            "exchange_account_id", "deployment_environment", "event_type", "venue_offer_id", "venue_seq",
            unique=True,
        ),
        CheckConstraint(
            "schema_version < 3 OR event_id IS NOT NULL",
            name="ck_event_log_v3_event_id",
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
        PrimaryKeyConstraint("exchange_account_id", "deployment_environment", "cid"),
        Index("idx_offer_claims_voi", "venue_offer_id"),
        Index(
            "uq_offer_claims_venue_offer_id",
            "exchange_account_id", "deployment_environment", "venue_offer_id",
            unique=True,
            postgresql_where=text("venue_offer_id IS NOT NULL"),
            sqlite_where=text("venue_offer_id IS NOT NULL"),
        ),
        Index(
            "uq_offer_claims_execution_decision_id",
            "exchange_account_id", "deployment_environment", "execution_decision_id",
            unique=True,
            postgresql_where=text("execution_decision_id IS NOT NULL"),
            sqlite_where=text("execution_decision_id IS NOT NULL"),
        ),
        # Only historical SQLite fixtures can have a NULL UUID owner.  The
        # production contract migration removes this compatibility surface;
        # keeping the fixture-only uniqueness prevents synthetic tests from
        # creating duplicate projections while the ORM models the final PK.
        Index(
            "uq_offer_claims_legacy_fixture_identity",
            "account_id", "deployment_environment", "cid",
            unique=True,
            sqlite_where=text("exchange_account_id IS NULL"),
            info={"identity_legacy_fixture": True},
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
    # Canonical v3 buckets.  The legacy ``reserved``/``realized`` columns stay
    # mapped during the staged migration so old rows remain readable until the
    # observation projector has backfilled these values and the old columns can
    # be dropped in the next Alembic revision.
    offered_amount: Mapped[Decimal] = mapped_column(
        Numeric, nullable=False, default=Decimal("0"), server_default=text("0")
    )
    lent_amount: Mapped[Decimal] = mapped_column(
        Numeric, nullable=False, default=Decimal("0"), server_default=text("0")
    )
    available_amount: Mapped[Decimal] = mapped_column(
        Numeric, nullable=False, default=Decimal("0"), server_default=text("0")
    )
    uncertain_amount: Mapped[Decimal] = mapped_column(
        Numeric, nullable=False, default=Decimal("0"), server_default=text("0")
    )
    last_venue_snapshot_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
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
        PrimaryKeyConstraint("exchange_account_id", "deployment_environment", "symbol"),
    )


class VenueOfferStateRow(Base):
    """Snapshot: one normalized venue offer per account/environment/offer ID."""

    __tablename__ = "venue_offer_state"

    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT", name="fk_venue_offer_state_account"),
        nullable=False,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    venue_offer_id: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    amount_original: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    amount_remaining: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    rate: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    # Keep the database vocabulary from the Bitfinex API while exposing the
    # explicit ``period_days`` domain name to Python callers.
    period_days: Mapped[int | None] = mapped_column("period", Integer, nullable=True)
    offer_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    flags: Mapped[dict[str, Any]] = mapped_column(
        _JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    mts_created: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mts_updated: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    execution_decision_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_seen_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_terminal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "exchange_account_id", "deployment_environment", "venue_offer_id"
        ),
        Index(
            "idx_venue_offer_state_account_status",
            "exchange_account_id", "deployment_environment", "status",
        ),
    )


class VenueCreditStateRow(Base):
    """Snapshot: one normalized venue credit per account/environment/credit ID."""

    __tablename__ = "venue_credit_state"

    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT", name="fk_venue_credit_state_account"),
        nullable=False,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    credit_id: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    rate: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    period_days: Mapped[int | None] = mapped_column("period", Integer, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    flags: Mapped[dict[str, Any]] = mapped_column(
        _JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    mts_created: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mts_updated: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    first_seen_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_seen_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_terminal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "exchange_account_id", "deployment_environment", "credit_id"
        ),
        Index(
            "idx_venue_credit_state_account_status",
            "exchange_account_id", "deployment_environment", "status",
        ),
    )


class ProjectionHeadRow(Base):
    """Cursor for one deterministic projector and account/environment stream."""

    __tablename__ = "projection_heads"

    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT", name="fk_projection_heads_account"),
        nullable=False,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    projection_name: Mapped[str] = mapped_column(Text, nullable=False)
    last_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    projector_version: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "exchange_account_id", "deployment_environment", "projection_name"
        ),
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
        Index(
            "idx_reconcile_obs_acct_env_symbol_id",
            "exchange_account_id", "deployment_environment", "symbol", "id",
        ),
    )
