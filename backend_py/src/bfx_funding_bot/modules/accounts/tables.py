from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    Text,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    plan: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'free'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (Index("idx_users_email", "email", unique=True),)


class ExchangeAccount(Base):
    """Canonical money-domain aggregate for one venue account.

    The UUID is assigned once and is the stable identity used by all later
    account-scoped tables.  Lifecycle transitions are represented by
    ``lifecycle_status``; an account with money history is never hard-deleted.
    """

    __tablename__ = "exchange_accounts"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    venue: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "lifecycle_status IN ('active', 'halted', 'retired')",
            name="ck_exchange_accounts_lifecycle_status",
        ),
        Index("idx_exchange_accounts_venue_status", "venue", "lifecycle_status"),
    )


@event.listens_for(ExchangeAccount.id, "set", retval=True)
def _reject_exchange_account_id_change(target: ExchangeAccount, value: UUID, oldvalue: object, _initiator: object) -> UUID:
    """Prevent changing a persisted aggregate identity in the ORM."""
    oldvalue_name = getattr(oldvalue, "name", None)
    if oldvalue is not None and oldvalue_name not in {"NO_VALUE", "NEVER_SET"} and value != oldvalue:
        raise ValueError("ExchangeAccount.id is immutable")
    return value


class ExchangeAccountMembership(Base):
    """Data-backed authorization membership for an exchange account."""

    __tablename__ = "exchange_account_memberships"

    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        PrimaryKeyConstraint("exchange_account_id", "user_id"),
        CheckConstraint(
            "role IN ('owner', 'operator', 'viewer')",
            name="ck_exchange_account_memberships_role",
        ),
        Index("idx_exchange_account_memberships_user", "user_id"),
    )


class ExchangeAccountCredential(Base):
    """Account-owned envelope-encrypted Bitfinex credential.

    Credential rows are lifecycle records.  The partial unique index prevents
    two active execution keys for the same account and venue while retaining
    retired/revoked rows for audit and rotation history.
    """

    __tablename__ = "exchange_account_credentials"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    venue: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    api_key: Mapped[str] = mapped_column(Text, nullable=False)
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    secret_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    dek_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    lifecycle_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_verify_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        CheckConstraint(
            "lifecycle_status IN ('active', 'revoked', 'retired')",
            name="ck_exchange_account_credentials_lifecycle_status",
        ),
        Index(
            "uq_exchange_account_credentials_active_venue",
            "exchange_account_id",
            "venue",
            unique=True,
            postgresql_where=text("lifecycle_status = 'active'"),
            sqlite_where=text("lifecycle_status = 'active'"),
        ),
        Index(
            "idx_exchange_account_credentials_account",
            "exchange_account_id",
            "created_at",
        ),
    )


_ACCOUNT_JSON = JSON().with_variant(JSONB, "postgresql")


class AccountConfigDraft(Base):
    """Account-scoped user-editable configuration draft.

    This row is intentionally not applied execution state.  ``revision`` is a
    monotonic application-owned version incremented by the config service.
    """

    __tablename__ = "account_config_drafts"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    config: Mapped[dict[str, Any]] = mapped_column(_ACCOUNT_JSON, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("uq_account_config_drafts_account", "exchange_account_id", unique=True),
    )


class LegacyAccountRealmMap(Base):
    """Auditable legacy realm-to-account mapping used only by Halt 1 tooling."""

    __tablename__ = "legacy_account_realm_map"

    realm_key: Mapped[str] = mapped_column(Text, primary_key=True)
    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class APIKey(Base):
    """SP2 vault row. One per user_profile.user_id. The Bitfinex api secret is
    stored envelope-encrypted (see core.crypto). The FK to user_profiles.user_id
    is enforced at the DB level by the migration ONLY (not as a model-level
    ForeignKey) — mirrors UserProfile, keeping Base.metadata.create_all() in
    tests free of cross-table ordering constraints.
    """

    __tablename__ = "api_keys"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,  # client-side: ORM supplies the uuid (works on sqlite tests)
        server_default=text("gen_random_uuid()"),  # PG DB-level default (raw SQL inserts)
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    exchange_account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    label: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    api_key: Mapped[str] = mapped_column(Text, nullable=False)
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    secret_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    dek_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    exchange_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'unverified'")
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_verify_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("idx_api_keys_user_id", "user_id", unique=True),
        Index("idx_api_keys_exchange_account", "exchange_account_id"),
    )


class UserConfig(Base):
    """Per-user strategy-preference config (SP3). One row per user.

    INERT in v1: the lending daemon never reads this table; cells.yaml remains
    the engine's only config source. This stores the customer knob for the FE
    and is groundwork for SP6 (engine consumption of per-tenant config).

    Identity matches the post-SP1 world: user_id is the Better Auth auth.user.id
    (TEXT), and — like UserProfile / APIKey — there is NO model-level ForeignKey
    (the FK to user_profiles.user_id is enforced by the migration only, so
    Base.metadata.create_all() in sqlite test fixtures doesn't need the auth
    schema). id carries both a client-side uuid4 default (sqlite tests have no
    gen_random_uuid()) and the Postgres server_default.
    """

    __tablename__ = "user_configs"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    exchange_account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    # JSONB on Postgres, generic JSON on sqlite for tests — bare-class variant
    # matches the event_store / diagnostics JSON columns' house style.
    config: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    # onupdate (unlike sibling UserProfile/APIKey) is intentional: this row is
    # re-saved on every config edit and the FE surfaces updatedAt, so it must
    # refresh. ORM-side only, which is fine — all writes go through the service.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    __table_args__ = (
        Index("idx_user_configs_user_id", "user_id", unique=True),
        Index("idx_user_configs_exchange_account", "exchange_account_id"),
    )


class Execution(Base):
    __tablename__ = "executions"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE", name="fk_executions_user"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    rate: Mapped[float] = mapped_column(Float, nullable=False)
    period: Mapped[int] = mapped_column(Integer, nullable=False)
    offer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'success'"))
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (Index("idx_executions_user_created", "user_id", "created_at"),)


class BillingRecord(Base):
    __tablename__ = "billing_records"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE", name="fk_billing_user"),
        nullable=False,
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    plan: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'free'"))
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'USD'"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (Index("idx_billing_user_period", "user_id", "period_start"),)
