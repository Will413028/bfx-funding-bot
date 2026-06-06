"""add auth schema (Better Auth) + user_profile

Revision ID: e61f3d1ed7ca
Revises: dac1e2f3a4b5
Create Date: 2026-06-06

Ports the Better Auth schema emitted by ``better-auth generate`` (better-auth
1.6.14, config ``frontend/src/lib/auth.ts``) into the ``auth`` Postgres schema.
Better Auth (TS) reads/writes these at runtime via ``search_path=auth``;
``better-auth migrate`` is NEVER run against the live DB — this hand-crafted
Alembic migration is the single source of schema truth.

Notes on the generated schema (cross-checked against a throwaway Postgres):
- NO ``session`` table: the Better Auth config routes sessions to Upstash
  (``secondaryStorage``), so sessions never touch Postgres. ``generate`` omits it.
- Plugin columns present: admin (role/banned/banReason/banExpires on user;
  twoFactorEnabled on user), twoFactor (verified), passkey (aaguid), jwt (jwks
  table incl. expiresAt). Column names are Better Auth's camelCase verbatim
  (SQLAlchemy quotes them).
- ``public.user_profiles`` (app-side SaaS profile) FKs ``auth.user.id`` (TEXT).
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlalchemy.dialects.postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e61f3d1ed7ca"
down_revision: str | Sequence[str] | None = "dac1e2f3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS auth")

    # --- Better Auth core: user ---
    op.create_table(
        "user",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("emailVerified", sa.Boolean(), nullable=False),
        sa.Column("image", sa.Text(), nullable=True),
        sa.Column(
            "createdAt",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updatedAt",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # admin plugin
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("banned", sa.Boolean(), nullable=True),
        sa.Column("banReason", sa.Text(), nullable=True),
        sa.Column("banExpires", sa.DateTime(timezone=True), nullable=True),
        # twoFactor plugin
        sa.Column("twoFactorEnabled", sa.Boolean(), nullable=True),
        sa.UniqueConstraint("email", name="user_email_key"),
        schema="auth",
    )

    # --- Better Auth core: account ---
    op.create_table(
        "account",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("accountId", sa.Text(), nullable=False),
        sa.Column("providerId", sa.Text(), nullable=False),
        sa.Column(
            "userId",
            sa.Text(),
            sa.ForeignKey("auth.user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("accessToken", sa.Text(), nullable=True),
        sa.Column("refreshToken", sa.Text(), nullable=True),
        sa.Column("idToken", sa.Text(), nullable=True),
        sa.Column("accessTokenExpiresAt", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refreshTokenExpiresAt", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("password", sa.Text(), nullable=True),
        sa.Column(
            "createdAt",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("updatedAt", sa.DateTime(timezone=True), nullable=False),
        schema="auth",
    )
    op.create_index(
        "account_userId_idx", "account", ["userId"], unique=False, schema="auth"
    )

    # --- Better Auth core: verification ---
    op.create_table(
        "verification",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("identifier", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("expiresAt", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "createdAt",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updatedAt",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        schema="auth",
    )
    op.create_index(
        "verification_identifier_idx",
        "verification",
        ["identifier"],
        unique=False,
        schema="auth",
    )

    # --- jwt plugin: jwks ---
    op.create_table(
        "jwks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("publicKey", sa.Text(), nullable=False),
        sa.Column("privateKey", sa.Text(), nullable=False),
        sa.Column("createdAt", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expiresAt", sa.DateTime(timezone=True), nullable=True),
        schema="auth",
    )

    # --- twoFactor plugin ---
    op.create_table(
        "twoFactor",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("backupCodes", sa.Text(), nullable=False),
        sa.Column(
            "userId",
            sa.Text(),
            sa.ForeignKey("auth.user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("verified", sa.Boolean(), nullable=True),
        schema="auth",
    )
    op.create_index(
        "twoFactor_userId_idx", "twoFactor", ["userId"], unique=False, schema="auth"
    )
    op.create_index(
        "twoFactor_secret_idx", "twoFactor", ["secret"], unique=False, schema="auth"
    )

    # --- passkey plugin ---
    op.create_table(
        "passkey",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("publicKey", sa.Text(), nullable=False),
        sa.Column(
            "userId",
            sa.Text(),
            sa.ForeignKey("auth.user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("credentialID", sa.Text(), nullable=False),
        sa.Column("counter", sa.Integer(), nullable=False),
        sa.Column("deviceType", sa.Text(), nullable=False),
        sa.Column("backedUp", sa.Boolean(), nullable=False),
        sa.Column("transports", sa.Text(), nullable=True),
        sa.Column("createdAt", sa.DateTime(timezone=True), nullable=True),
        sa.Column("aaguid", sa.Text(), nullable=True),
        schema="auth",
    )
    op.create_index(
        "passkey_userId_idx", "passkey", ["userId"], unique=False, schema="auth"
    )
    op.create_index(
        "passkey_credentialID_idx",
        "passkey",
        ["credentialID"],
        unique=False,
        schema="auth",
    )

    # --- public.user_profiles (Task 6): app-side SaaS profile keyed to auth.user ---
    op.create_table(
        "user_profiles",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey(
                "auth.user.id", ondelete="CASCADE", name="fk_user_profiles_user"
            ),
            nullable=False,
        ),
        sa.Column(
            "plan", sa.Text(), nullable=False, server_default=sa.text("'free'")
        ),
        sa.Column("org_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        schema="public",
    )
    op.create_index(
        "idx_user_profiles_user_id",
        "user_profiles",
        ["user_id"],
        unique=True,
        schema="public",
    )


def downgrade() -> None:
    # user_profiles first (FK -> auth.user), then auth tables, then the schema.
    op.drop_index(
        "idx_user_profiles_user_id", table_name="user_profiles", schema="public"
    )
    op.drop_table("user_profiles", schema="public")
    for tbl in ("passkey", "twoFactor", "jwks", "verification", "account", "user"):
        op.drop_table(tbl, schema="auth")
    op.execute("DROP SCHEMA IF EXISTS auth")
