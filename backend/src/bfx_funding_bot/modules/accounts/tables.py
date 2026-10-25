from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
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
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import JSON_DOCUMENT, Base


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
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
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
        Text, nullable=False, server_default=text("'pending'")
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_verify_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
    # The operator rotation that made this credential active (``rotation_tables``); NULL for
    # a bootstrapped credential (or one the former web API endpoints wrote). ``use_alter``: the
    # request names the credential it replaces, so the two tables reference each other.
    operator_request_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("credential_rotation_requests.request_id", ondelete="RESTRICT",
                   name="fk_exchange_account_credentials_operator_request", use_alter=True),
        nullable=True,
    )
    # The Bitfinex account this key belongs to (``v2/auth/r/info/user`` column 0). Written by
    # an applied rotation and the operator bootstrap when the row is created; a rotation
    # compares the new key's account with it, so a key already revoked at Bitfinex can still
    # be replaced. An active row always has it (``..._active_user_id``); NULL only on an
    # inactive row written before the id was stored.
    bitfinex_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "lifecycle_status IN ('pending', 'active', 'revoked', 'retired')",
            name="ck_exchange_account_credentials_lifecycle_status",
        ),
        CheckConstraint(
            "lifecycle_status <> 'active' OR "
            "(verified_at IS NOT NULL AND last_verify_error IS NULL)",
            name="ck_exchange_account_credentials_active_verified",
        ),
        CheckConstraint(
            "bitfinex_user_id IS NULL OR bitfinex_user_id > 0",
            name="ck_exchange_account_credentials_bitfinex_user_id_positive",
        ),
        CheckConstraint(
            "lifecycle_status <> 'active' OR bitfinex_user_id IS NOT NULL",
            name="ck_exchange_account_credentials_active_user_id",
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


_ACCOUNT_JSON = JSON_DOCUMENT


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
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
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


class UserConfig(Base):
    """Per-user strategy-preference config (SP3). One row per user.

    INERT in v1: the lending daemon never reads this table; cells.yaml remains
    the engine's only config source. This stores the customer knob for the FE
    and is groundwork for SP6 (engine consumption of per-tenant config).

    Identity matches the post-SP1 world: user_id is the (retired) Better Auth
    user ID (TEXT), and there is NO model-level ForeignKey
    (the FK to user_profiles.user_id is enforced by the migration only, keeping
    Base.metadata.create_all() in sqlite test fixtures free of cross-table
    ordering constraints). id carries both a client-side uuid4 default (sqlite tests have no
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
    config: Mapped[dict[str, Any]] = mapped_column(_ACCOUNT_JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    # onupdate (unlike sibling UserProfile) is intentional: this row is
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
