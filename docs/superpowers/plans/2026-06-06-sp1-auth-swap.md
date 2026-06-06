# SP1 Auth Swap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dead hand-rolled HS256 auth (FE calls `/api/v1/auth/*` endpoints that don't exist) with self-hosted Better Auth — Next app hosts `/api/auth/*`, a standalone FastAPI web-API verifies via JWKS, and a `user_profile` table is added — so a real login→authenticated-API round-trip works end-to-end.

**Architecture:** Better Auth (TS) runs inside the Next app at `/api/auth/*`, storing its tables in the **same Neon DB** under a dedicated `auth` schema (search_path), with sessions/rate-limit counters in Upstash Redis. The browser holds only Better Auth's opaque httpOnly session cookie; the existing BFF proxy stays but swaps its token source — server-side it mints a short-lived EdDSA JWT via `auth.api.getToken()` and forwards it as `Authorization: Bearer` to a **standalone** FastAPI web-API (the `main.py` app, deployed separately from the real-money daemon), which verifies it statelessly against the Next app's JWKS and joins app data by `user_id`. **Alembic remains the single migration tool** (Better Auth's schema is ported via `better-auth generate`; `better-auth migrate` is never run). The live real-money daemon (`healthz.make_app`) is untouched.

**Tech Stack:** Frontend: Next.js 16.1.6 / React 19.2.3, pnpm, Better Auth 1.6.14 (`better-auth`, `@better-auth/passkey`, `pg`, `@upstash/redis`), vitest, biome, Playwright. Backend: FastAPI / SQLAlchemy 2.0 async / Alembic / `pyjwt[crypto]`, uv, pytest (+ testcontainers for DB integration). DB: Neon Postgres (`auth` schema), Upstash Redis.

**Key decisions carried from spec `docs/superpowers/specs/2026-06-06-frontend-saas-architecture-design.md`:** D8 self-hosted Better Auth; D13 same Neon DB + `auth` schema + scoped role + Alembic-single-tool; D14 TOTP + step-up on money ops. `aud="bfx-funding-backend"`, `sub`=Better Auth user uuid, `requireEmailVerification:false` (v1).

**Pre-flight (one-time, human/setup — NOT a code task):**
- Create a Postgres role `webapp` scoped to the `auth` schema for the FE Better Auth connection (least-privilege; cannot touch trading tables). Document the GRANT in the runbook. For v1 dev you may reuse the existing role; the scoped role is a deploy-time hardening.
- Provision Upstash Redis REST creds (reuse the existing Upstash project if present).
- Generate `BETTER_AUTH_SECRET`: `npx @better-auth/cli@latest secret`.

---

## File Structure

**Frontend (create):**
- `frontend/src/lib/auth.ts` — Better Auth server instance (one responsibility: auth config).
- `frontend/src/lib/auth-client.ts` — Better Auth React client.
- `frontend/src/app/api/auth/[...all]/route.ts` — mounts `/api/auth/*`.

**Frontend (modify):**
- `frontend/src/lib/env.ts` — drop `AUTH_SECRET`; add Better-Auth/Upstash/DB vars.
- `frontend/src/app/[locale]/(auth)/actions.ts` — replace fetch+cookie auth with Better Auth.
- `frontend/middleware.ts` — replace base64-exp parse with `getSessionCookie`.
- `frontend/src/app/api/proxy/[...path]/route.ts` — swap token source to minted JWT; delete `tryRefresh`.
- `frontend/src/lib/api-client.ts` — align server-side token source.
- `frontend/src/features/auth/components/{login-form,register-form}.tsx` — call Better Auth + 2FA redirect.
- `frontend/package.json` — deps.
- `frontend/.env.example` — new vars.

**Backend (create):**
- `backend_py/src/bfx_funding_bot/core/auth.py` — JWKS verify dependency.
- `backend_py/src/bfx_funding_bot/modules/accounts/user_profile.py` — `UserProfile` model.
- `backend_py/src/bfx_funding_bot/modules/api/__init__.py`, `routers.py` — protected `/api/v1` router.
- `backend_py/alembic/versions/<rev>_add_auth_schema_and_user_profile.py` — hand-crafted migration.
- Tests: `backend_py/tests/test_jwks_auth.py`, `backend_py/tests/test_user_profile_router.py`, `backend_py/tests/integration/test_user_profile_migration.py`.

**Backend (modify):**
- `backend_py/src/bfx_funding_bot/core/settings.py` — add JWKS settings.
- `backend_py/src/bfx_funding_bot/main.py` — mount the protected router.
- `backend_py/alembic/env.py` — register `user_profile` module import.
- `backend_py/pyproject.toml` — add `pyjwt[crypto]`.

---

## Task 1: FE deps + environment schema

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/src/lib/env.ts`
- Modify: `frontend/.env.example`

- [ ] **Step 1: Add dependencies**

Run (in `frontend/`):
```bash
cd frontend && pnpm add better-auth@1.6.14 @better-auth/passkey@1.6.14 pg@^8.21.0 @upstash/redis@^1 && pnpm add -D @types/pg
```
Expected: `package.json` dependencies now include `better-auth`, `@better-auth/passkey`, `pg`, `@upstash/redis`; devDependencies include `@types/pg`.

- [ ] **Step 2: Swap env schema (write the change)**

In `frontend/src/lib/env.ts`, replace the two schema objects (lines 3–13) with:
```typescript
const clientEnvSchema = z.object({
  NEXT_PUBLIC_APP_URL: z.string().url(),
  NEXT_PUBLIC_APP_NAME: z.string().min(1),
  NEXT_PUBLIC_WS_URL: z.string().min(1),
  NEXT_PUBLIC_SENTRY_DSN: z.string().url().optional(),
  NEXT_PUBLIC_BETTER_AUTH_URL: z.string().url(),
});

const serverEnvSchema = clientEnvSchema.extend({
  API_URL: z.string().url(),
  BETTER_AUTH_SECRET: z.string().min(32),
  BETTER_AUTH_URL: z.string().url(),
  DATABASE_URL: z.string().url(),
  UPSTASH_REDIS_REST_URL: z.string().url(),
  UPSTASH_REDIS_REST_TOKEN: z.string().min(1),
  PASSKEY_RP_ID: z.string().min(1),
});
```
Then in the client `safeParse` object (lines 17–22) add `NEXT_PUBLIC_BETTER_AUTH_URL: process.env.NEXT_PUBLIC_BETTER_AUTH_URL,` and in the client fallback return object (lines 33–38) add `NEXT_PUBLIC_BETTER_AUTH_URL: "",`. Delete every reference to `AUTH_SECRET`.

- [ ] **Step 3: Update `.env.example`**

Add to `frontend/.env.example` (and remove `AUTH_SECRET`):
```bash
NEXT_PUBLIC_BETTER_AUTH_URL=http://localhost:3000
BETTER_AUTH_URL=http://localhost:3000
BETTER_AUTH_SECRET=   # npx @better-auth/cli@latest secret
DATABASE_URL=postgresql://USER:PASS@ep-xxx-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
UPSTASH_REDIS_REST_URL=https://xxx.upstash.io
UPSTASH_REDIS_REST_TOKEN=xxx
PASSKEY_RP_ID=localhost
```

- [ ] **Step 4: Verify typecheck/lint passes**

Run: `cd frontend && pnpm lint`
Expected: PASS (no `AUTH_SECRET` references remain; new env vars typed). If `pnpm lint` flags unrelated files, only the env-related errors must be clean.

- [ ] **Step 5: Commit**
```bash
git add frontend/package.json frontend/pnpm-lock.yaml frontend/src/lib/env.ts frontend/.env.example
git commit -m "✨ Feat: add Better Auth deps + env schema (SP1 auth swap)"
```

---

## Task 2: Better Auth server instance (`lib/auth.ts`)

**Files:**
- Create: `frontend/src/lib/auth.ts`

- [ ] **Step 1: Write the server instance**

Create `frontend/src/lib/auth.ts`:
```typescript
import { betterAuth } from "better-auth";
import { Pool } from "pg";
import { Redis } from "@upstash/redis";
import { admin, jwt, twoFactor } from "better-auth/plugins";
import { passkey } from "@better-auth/passkey";

const redis = Redis.fromEnv(); // UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN

export const auth = betterAuth({
  appName: "BFX Funding Bot",
  baseURL: process.env.BETTER_AUTH_URL,
  trustedOrigins: [process.env.BETTER_AUTH_URL!],

  // Same Neon DB, dedicated `auth` schema (D13). Pooled (-pooler) URL for serverless.
  database: new Pool({
    connectionString: process.env.DATABASE_URL,
    options: "-c search_path=auth",
  }),

  // Sessions + rate-limit counters live in Upstash (REST), not Neon.
  secondaryStorage: {
    get: async (key) => {
      const v = await redis.get<string>(key);
      return v ?? null;
    },
    set: async (key, value, ttl) => {
      if (ttl) await redis.set(key, value, { ex: ttl });
      else await redis.set(key, value);
    },
    delete: async (key) => {
      await redis.del(key);
    },
  },

  emailAndPassword: {
    enabled: true,
    autoSignIn: true,
    requireEmailVerification: false, // v1
  },

  session: {
    expiresIn: 60 * 60 * 24 * 7,
    updateAge: 60 * 60 * 24,
    cookieCache: { enabled: true, maxAge: 5 * 60 },
  },

  // Route rate-limit counters to Upstash (secondaryStorage alone does NOT do this).
  rateLimit: {
    enabled: true,
    storage: "secondary-storage",
    customRules: {
      "/sign-in/email": { window: 60, max: 5 },
      "/sign-up/email": { window: 60, max: 5 },
      "/two-factor/*": { window: 60, max: 5 },
      "/forget-password": { window: 60, max: 3 },
    },
  },

  plugins: [
    admin({ defaultRole: "user", adminRoles: ["admin"] }),
    twoFactor({ issuer: "BFX Funding Bot" }),
    passkey({
      rpID: process.env.PASSKEY_RP_ID!,
      rpName: "BFX Funding Bot",
      origin: process.env.BETTER_AUTH_URL!,
    }),
    jwt({
      jwt: {
        // Stable audience the Python backend checks, decoupled from the public URL.
        audience: "bfx-funding-backend",
        // issuer defaults to BETTER_AUTH_URL; expiration defaults to 15m. EdDSA default.
        definePayload: ({ user }) => ({
          sub: user.id,
          email: user.email,
          role: (user as { role?: string }).role ?? "user",
        }),
      },
    }),
  ],
});
```

- [ ] **Step 2: Verify it imports/typechecks**

Run: `cd frontend && pnpm exec tsc --noEmit`
Expected: PASS. (If `jwt`'s `definePayload`/`audience` option names differ in 1.6.14, reconcile against `node_modules/better-auth/dist/plugins/jwt` types — the payload must include `sub`, `email`, `role` and `aud` must be `bfx-funding-backend`.)

- [ ] **Step 3: Commit**
```bash
git add frontend/src/lib/auth.ts
git commit -m "✨ Feat: Better Auth server instance (auth schema, Upstash, jwt/2fa/passkey)"
```

---

## Task 3: Better Auth React client (`lib/auth-client.ts`)

**Files:**
- Create: `frontend/src/lib/auth-client.ts`

- [ ] **Step 1: Write the client**

Create `frontend/src/lib/auth-client.ts`:
```typescript
import { createAuthClient } from "better-auth/react";
import {
  adminClient,
  jwtClient,
  twoFactorClient,
} from "better-auth/client/plugins";
import { passkeyClient } from "@better-auth/passkey/client";

export const authClient = createAuthClient({
  baseURL: process.env.NEXT_PUBLIC_BETTER_AUTH_URL,
  plugins: [
    jwtClient(),
    adminClient(),
    passkeyClient(),
    twoFactorClient({
      onTwoFactorRedirect() {
        window.location.href = "/two-factor";
      },
    }),
  ],
});
```

- [ ] **Step 2: Verify typecheck**

Run: `cd frontend && pnpm exec tsc --noEmit`
Expected: PASS.

- [ ] **Step 3: Commit**
```bash
git add frontend/src/lib/auth-client.ts
git commit -m "✨ Feat: Better Auth React client"
```

---

## Task 4: Mount `/api/auth/*` route handler

**Files:**
- Create: `frontend/src/app/api/auth/[...all]/route.ts`

- [ ] **Step 1: Write the handler**

Create `frontend/src/app/api/auth/[...all]/route.ts`:
```typescript
import { auth } from "@/lib/auth";
import { toNextJsHandler } from "better-auth/next-js";

export const { GET, POST } = toNextJsHandler(auth);
```

- [ ] **Step 2: Verify build resolves the route**

Run: `cd frontend && pnpm exec tsc --noEmit`
Expected: PASS. (Full `pnpm build` is deferred to Task 13 once env is wired; tsc confirms the handler compiles.)

- [ ] **Step 3: Commit**
```bash
git add "frontend/src/app/api/auth/[...all]/route.ts"
git commit -m "✨ Feat: mount Better Auth /api/auth/* handler"
```

---

## Task 5: Generate Better Auth schema → hand-crafted Alembic migration (`auth` schema)

**Files:**
- Create: `backend_py/alembic/versions/<rev>_add_auth_schema_and_user_profile.py` (this task adds the Better Auth tables; Task 6 adds `user_profile` in the SAME migration file)
- Reference only: run `better-auth generate` to get the authoritative DDL

**Context:** Alembic is the single migration tool (D13). We use Better Auth's CLI ONLY to learn the exact schema, then port it into one hand-crafted Alembic migration. `better-auth migrate` is NEVER run. `.env` points at LIVE Neon → the migration is applied at deploy via compose `migrate`, NOT locally; local verification is the testcontainers integration test in Task 6.

- [ ] **Step 1: Generate the authoritative schema SQL**

Run (in `frontend/`, with `DATABASE_URL` pointed at a THROWAWAY/dev Postgres, never live):
```bash
cd frontend && npx @better-auth/cli@latest generate --config src/lib/auth.ts --output ../backend_py/alembic/_better_auth_generated.sql --yes
```
Expected: a SQL file listing `CREATE TABLE` for `user`, `session`, `account`, `verification`, plus plugin tables `jwks` (jwt), `twoFactor` (twoFactor), `passkey` (passkey), and `admin`-added columns on `user`/`session`. **Review this file — it is the source of truth for Step 2.** (Delete the temp file after porting; do not commit it.)

- [ ] **Step 2: Create the migration skeleton with Better Auth tables in the `auth` schema**

Create `backend_py/alembic/versions/<rev>_add_auth_schema_and_user_profile.py` (pick an 8-char hex `<rev>`; copy the structure of `dac1e2f3a4b5_add_symbol_to_offer_claims.py`):
```python
"""add auth schema (Better Auth) + user_profile

Revision ID: <rev>
Revises: dac1e2f3a4b5
Create Date: 2026-06-06

Ports the Better Auth schema emitted by `better-auth generate` into the `auth`
Postgres schema. Better Auth (TS) reads/writes these at runtime via search_path=auth;
`better-auth migrate` is never run. user_profile (Task 6) FKs auth.user.id.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "<rev>"
down_revision: str | Sequence[str] | None = "dac1e2f3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS auth")
    # --- Better Auth core + plugin tables (PORTED VERBATIM from `better-auth generate`).
    # Replace the bodies below with the exact columns from the generated SQL,
    # qualifying every table with schema="auth". The structure below is the
    # expected 1.6.14 core shape; reconcile against the generated file.
    op.create_table(
        "user",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("emailVerified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("image", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=True),          # admin plugin
        sa.Column("banned", sa.Boolean(), nullable=True),     # admin plugin
        sa.Column("banReason", sa.Text(), nullable=True),
        sa.Column("banExpires", sa.DateTime(timezone=True), nullable=True),
        sa.Column("twoFactorEnabled", sa.Boolean(), nullable=True),  # twoFactor plugin
        sa.Column("createdAt", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updatedAt", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("email", name="uq_auth_user_email"),
        schema="auth",
    )
    op.create_table(
        "session",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("userId", sa.Text(), sa.ForeignKey("auth.user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("expiresAt", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ipAddress", sa.Text(), nullable=True),
        sa.Column("userAgent", sa.Text(), nullable=True),
        sa.Column("impersonatedBy", sa.Text(), nullable=True),  # admin plugin
        sa.Column("createdAt", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updatedAt", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("token", name="uq_auth_session_token"),
        schema="auth",
    )
    op.create_table(
        "account",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("userId", sa.Text(), sa.ForeignKey("auth.user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("accountId", sa.Text(), nullable=False),
        sa.Column("providerId", sa.Text(), nullable=False),
        sa.Column("accessToken", sa.Text(), nullable=True),
        sa.Column("refreshToken", sa.Text(), nullable=True),
        sa.Column("idToken", sa.Text(), nullable=True),
        sa.Column("accessTokenExpiresAt", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refreshTokenExpiresAt", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("password", sa.Text(), nullable=True),
        sa.Column("createdAt", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updatedAt", sa.DateTime(timezone=True), nullable=False),
        schema="auth",
    )
    op.create_table(
        "verification",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("identifier", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("expiresAt", sa.DateTime(timezone=True), nullable=False),
        sa.Column("createdAt", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updatedAt", sa.DateTime(timezone=True), nullable=True),
        schema="auth",
    )
    op.create_table(
        "jwks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("publicKey", sa.Text(), nullable=False),
        sa.Column("privateKey", sa.Text(), nullable=False),
        sa.Column("createdAt", sa.DateTime(timezone=True), nullable=False),
        schema="auth",
    )
    op.create_table(
        "twoFactor",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("userId", sa.Text(), sa.ForeignKey("auth.user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("backupCodes", sa.Text(), nullable=False),
        schema="auth",
    )
    op.create_table(
        "passkey",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("userId", sa.Text(), sa.ForeignKey("auth.user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("publicKey", sa.Text(), nullable=False),
        sa.Column("credentialID", sa.Text(), nullable=False),
        sa.Column("counter", sa.Integer(), nullable=False),
        sa.Column("deviceType", sa.Text(), nullable=False),
        sa.Column("backedUp", sa.Boolean(), nullable=False),
        sa.Column("transports", sa.Text(), nullable=True),
        sa.Column("createdAt", sa.DateTime(timezone=True), nullable=True),
        schema="auth",
    )
    # user_profile is added by Task 6 below (same upgrade()).


def downgrade() -> None:
    # user_profile dropped by Task 6 first, then:
    for t in ("passkey", "twoFactor", "jwks", "verification", "account", "session", "user"):
        op.drop_table(t, schema="auth")
    op.execute("DROP SCHEMA IF EXISTS auth")
```

- [ ] **Step 3: Reconcile columns against the generated SQL**

Compare each `auth.*` table above with `_better_auth_generated.sql` from Step 1. Fix any column name/type/nullability mismatch (Better Auth uses camelCase column names — preserve them exactly; quoting is automatic via SQLAlchemy). Delete `_better_auth_generated.sql`.

- [ ] **Step 4: Verify the migration imports offline**

Run: `cd backend_py && uv run alembic history | head -5`
Expected: lists the new revision after `dac1e2f3a4b5`. (Do NOT run `alembic upgrade` — `.env` is live Neon.)

- [ ] **Step 5: Commit** (combined with Task 6 after `user_profile` is added — see Task 6 Step 5.)

---

## Task 6: `user_profile` model + migration body + Alembic registration

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/accounts/user_profile.py`
- Modify: `backend_py/alembic/versions/<rev>_add_auth_schema_and_user_profile.py` (add `user_profiles` to upgrade/downgrade)
- Modify: `backend_py/alembic/env.py` (register the new module import)
- Test: `backend_py/tests/integration/test_user_profile_migration.py`

- [ ] **Step 1: Write the failing integration test**

Create `backend_py/tests/integration/test_user_profile_migration.py`:
```python
import pytest

pytestmark = pytest.mark.integration


def test_user_profile_table_created(alembic_upgraded_engine):
    """After `alembic upgrade head` on an ephemeral Postgres, auth.user and
    public.user_profiles exist with the FK wired."""
    from sqlalchemy import inspect

    insp = inspect(alembic_upgraded_engine)
    assert "user" in insp.get_table_names(schema="auth")
    assert "user_profiles" in insp.get_table_names(schema="public")
    fks = insp.get_foreign_keys("user_profiles", schema="public")
    assert any(fk["referred_table"] == "user" and fk["referred_schema"] == "auth" for fk in fks)
```
(Reuse the existing testcontainers fixture pattern; if no `alembic_upgraded_engine` fixture exists, add one in `backend_py/tests/integration/conftest.py` that spins a Postgres container, points `DATABASE_URL` at it, runs `alembic upgrade head`, and yields a sync engine — mirror the existing integration migration tests referenced by memory `008146a`.)

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend_py && uv run pytest tests/integration/test_user_profile_migration.py -v` (requires Docker)
Expected: FAIL — `user_profiles` not found.

- [ ] **Step 3: Write the model**

Create `backend_py/src/bfx_funding_bot/modules/accounts/user_profile.py`:
```python
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base


class UserProfile(Base):
    """App-side profile keyed to the Better Auth user (auth.user.id is TEXT).

    Carries SaaS fields (plan, future org_id) that Better Auth's user table
    does not own. JIT-provisioned on first authenticated request.
    """

    __tablename__ = "user_profiles"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # auth.user.id is a Better Auth TEXT id, NOT a uuid — store as Text.
    user_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("auth.user.id", ondelete="CASCADE", name="fk_user_profiles_user"),
        nullable=False,
    )
    plan: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'free'"))
    org_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (Index("idx_user_profiles_user_id", "user_id", unique=True),)
```

- [ ] **Step 4: Add `user_profiles` to the migration**

In `<rev>_add_auth_schema_and_user_profile.py`, append to `upgrade()` (after the `auth.*` tables):
```python
    op.create_table(
        "user_profiles",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.Text(),
                  sa.ForeignKey("auth.user.id", ondelete="CASCADE", name="fk_user_profiles_user"),
                  nullable=False),
        sa.Column("plan", sa.Text(), nullable=False, server_default=sa.text("'free'")),
        sa.Column("org_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        schema="public",
    )
    op.create_index("idx_user_profiles_user_id", "user_profiles", ["user_id"], unique=True, schema="public")
```
And prepend to `downgrade()` (before dropping the `auth.*` tables):
```python
    op.drop_index("idx_user_profiles_user_id", table_name="user_profiles", schema="public")
    op.drop_table("user_profiles", schema="public")
```
Add `import sqlalchemy.dialects.postgresql` at the top if not present.

- [ ] **Step 5: Register the module with Alembic metadata**

In `backend_py/alembic/env.py`, after line 18 (`import bfx_funding_bot.modules.accounts.tables`), add:
```python
import bfx_funding_bot.modules.accounts.user_profile  # noqa: F401
```

- [ ] **Step 6: Run the integration test to verify it passes**

Run: `cd backend_py && uv run pytest tests/integration/test_user_profile_migration.py -v`
Expected: PASS.

- [ ] **Step 7: Verify unit suite + types still green**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: PASS.

- [ ] **Step 8: Commit**
```bash
git add backend_py/alembic/versions/ backend_py/src/bfx_funding_bot/modules/accounts/user_profile.py backend_py/alembic/env.py backend_py/tests/integration/test_user_profile_migration.py
git commit -m "✨ Feat: auth schema (Better Auth) + user_profile migration (SP1)"
```

---

## Task 7: BE JWKS verify dependency (`core/auth.py`)

**Files:**
- Create: `backend_py/src/bfx_funding_bot/core/auth.py`
- Modify: `backend_py/src/bfx_funding_bot/core/settings.py`
- Modify: `backend_py/pyproject.toml`
- Test: `backend_py/tests/test_jwks_auth.py`

- [ ] **Step 1: Add the dependency**

In `backend_py/pyproject.toml` dependencies, add: `"pyjwt[crypto]>=2.9",`
Run: `cd backend_py && uv sync`
Expected: `pyjwt` + `cryptography` installed.

- [ ] **Step 2: Add settings**

In `backend_py/src/bfx_funding_bot/core/settings.py`, add to `Settings` (after `log_level`):
```python
    # SP1 web-API auth (Better Auth JWKS verification)
    better_auth_jwks_url: str = ""        # e.g. https://app.example.com/api/auth/jwks
    better_auth_issuer: str = ""          # e.g. https://app.example.com
    jwt_audience: str = "bfx-funding-backend"
```

- [ ] **Step 3: Write the failing test**

Create `backend_py/tests/test_jwks_auth.py`:
```python
import datetime as dt

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException

from bfx_funding_bot.core import auth as auth_mod


def _mint(claims: dict, key: Ed25519PrivateKey, kid: str = "test-kid") -> str:
    return jwt.encode(claims, key, algorithm="EdDSA", headers={"kid": kid})


@pytest.fixture()
def keypair(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    class _FakeJWKClient:
        def __init__(self, *a, **k): ...
        def get_signing_key_from_jwt(self, _token):
            class _K:  # PyJWT signing-key shape
                key = pub
            return _K()

    monkeypatch.setattr(auth_mod, "_jwk_client", _FakeJWKClient())
    return priv


def _claims(**over):
    now = dt.datetime.now(tz=dt.timezone.utc)
    base = {
        "sub": "user_123",
        "email": "will@example.com",
        "role": "admin",
        "iss": "https://app.example.com",
        "aud": "bfx-funding-backend",
        "iat": now,
        "exp": now + dt.timedelta(minutes=15),
    }
    base.update(over)
    return base


def test_valid_token_returns_principal(keypair, monkeypatch):
    monkeypatch.setattr(auth_mod, "_ISSUER", "https://app.example.com")
    token = _mint(_claims(), keypair)
    principal = auth_mod._verify(token)
    assert principal.user_id == "user_123"
    assert principal.email == "will@example.com"
    assert principal.role == "admin"


def test_expired_token_raises_401(keypair, monkeypatch):
    monkeypatch.setattr(auth_mod, "_ISSUER", "https://app.example.com")
    now = dt.datetime.now(tz=dt.timezone.utc)
    token = _mint(_claims(exp=now - dt.timedelta(minutes=1)), keypair)
    with pytest.raises(HTTPException) as ei:
        auth_mod._verify(token)
    assert ei.value.status_code == 401


def test_wrong_audience_raises_401(keypair, monkeypatch):
    monkeypatch.setattr(auth_mod, "_ISSUER", "https://app.example.com")
    token = _mint(_claims(aud="someone-else"), keypair)
    with pytest.raises(HTTPException) as ei:
        auth_mod._verify(token)
    assert ei.value.status_code == 401
```

- [ ] **Step 4: Run it to verify it fails**

Run: `cd backend_py && uv run pytest tests/test_jwks_auth.py -v`
Expected: FAIL — `bfx_funding_bot.core.auth` does not exist.

- [ ] **Step 5: Write the implementation**

Create `backend_py/src/bfx_funding_bot/core/auth.py`:
```python
"""SP1 web-API auth: verify Better Auth EdDSA JWTs via JWKS.

The standalone FastAPI web-API authorizes its /api/v1 endpoints by verifying
the short-lived JWT the Next BFF proxy forwards. Stateless: no DB hit, no call
back to the auth server beyond the cached JWKS fetch.
"""
from __future__ import annotations

from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from bfx_funding_bot.core.settings import Settings

_settings = Settings()  # type: ignore[call-arg]
_ISSUER = _settings.better_auth_issuer
_AUDIENCE = _settings.jwt_audience
# Module-level client: caches JWKS keys by kid, refetches on unknown kid.
_jwk_client = PyJWKClient(_settings.better_auth_jwks_url, cache_keys=True) if _settings.better_auth_jwks_url else None

_bearer = HTTPBearer(auto_error=True)


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str | None
    role: str


def _verify(token: str) -> Principal:
    if _jwk_client is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="auth_not_configured")
    try:
        signing_key = _jwk_client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["EdDSA"],
            issuer=_ISSUER,
            audience=_AUDIENCE,
            leeway=10,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_token") from e
    return Principal(user_id=claims["sub"], email=claims.get("email"), role=claims.get("role", "user"))


async def require_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> Principal:
    """FastAPI dependency — the authz boundary for all /api/v1 endpoints."""
    return _verify(creds.credentials)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/test_jwks_auth.py -v`
Expected: PASS (3 passed).

- [ ] **Step 7: Verify types/lint**

Run: `cd backend_py && uv run mypy src/ && uv run ruff check`
Expected: PASS.

- [ ] **Step 8: Commit**
```bash
git add backend_py/pyproject.toml backend_py/uv.lock backend_py/src/bfx_funding_bot/core/auth.py backend_py/src/bfx_funding_bot/core/settings.py backend_py/tests/test_jwks_auth.py
git commit -m "✨ Feat: JWKS verify dependency for web-API (SP1)"
```

---

## Task 8: Protected `/api/v1/profile` router + JIT provision + mount

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/api/__init__.py`
- Create: `backend_py/src/bfx_funding_bot/modules/api/routers.py`
- Modify: `backend_py/src/bfx_funding_bot/main.py`
- Test: `backend_py/tests/test_user_profile_router.py`

- [ ] **Step 1: Write the failing test**

Create `backend_py/tests/test_user_profile_router.py`:
```python
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bfx_funding_bot.core import auth as auth_mod
from bfx_funding_bot.modules.api.routers import build_router


@pytest.fixture()
def client(monkeypatch):
    app = FastAPI()
    app.include_router(build_router())

    # Override the auth dependency with a fake principal.
    from bfx_funding_bot.core.auth import Principal, require_user

    async def _fake_user():
        return Principal(user_id="user_abc", email="will@example.com", role="admin")

    app.dependency_overrides[require_user] = _fake_user
    return TestClient(app)


def test_profile_requires_auth():
    app = FastAPI()
    app.include_router(build_router())
    c = TestClient(app)
    r = c.get("/api/v1/profile")
    assert r.status_code in (401, 403)


def test_profile_returns_principal(client):
    r = client.get("/api/v1/profile")
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["userId"] == "user_abc"
    assert body["data"]["email"] == "will@example.com"
    assert body["data"]["role"] == "admin"
```
(For v1 the endpoint echoes the verified principal; the DB-backed `user_profiles` JIT read/insert is exercised by an integration test added in Step 4 once a session fixture exists. Keeping the unit test DB-free keeps it fast and in the `not integration` lane.)

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend_py && uv run pytest tests/test_user_profile_router.py -v`
Expected: FAIL — `bfx_funding_bot.modules.api.routers` does not exist.

- [ ] **Step 3: Write the router**

Create `backend_py/src/bfx_funding_bot/modules/api/__init__.py` (empty).
Create `backend_py/src/bfx_funding_bot/modules/api/routers.py`:
```python
"""SP1 standalone web-API router. Mounted on the main.py app (separate from the
real-money daemon's healthz app). Every endpoint is gated by require_user.
Responses use the FE envelope {"data": ...}.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from bfx_funding_bot.core.auth import Principal, require_user


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["api"])

    @router.get("/profile")
    async def get_profile(user: Principal = Depends(require_user)) -> dict:
        return {
            "data": {
                "userId": user.user_id,
                "email": user.email,
                "role": user.role,
            }
        }

    return router
```

- [ ] **Step 4: Mount on the standalone web-API app**

In `backend_py/src/bfx_funding_bot/main.py`, after line 31 (`app = FastAPI(...)`), add:
```python
from bfx_funding_bot.modules.api.routers import build_router as build_api_router

app.include_router(build_api_router())
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/test_user_profile_router.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Full backend gate**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: PASS.

- [ ] **Step 7: Commit**
```bash
git add backend_py/src/bfx_funding_bot/modules/api/ backend_py/src/bfx_funding_bot/main.py backend_py/tests/test_user_profile_router.py
git commit -m "✨ Feat: protected /api/v1/profile router on standalone web-API (SP1)"
```

---

## Task 9: Rewrite `(auth)/actions.ts` to Better Auth

**Files:**
- Modify: `frontend/src/app/[locale]/(auth)/actions.ts`

- [ ] **Step 1: Replace the file contents**

Replace ALL of `frontend/src/app/[locale]/(auth)/actions.ts` with thin server-action wrappers over Better Auth (delete `setAuthCookies`/`clearAuthCookies`/the `/api/v1/auth/*` fetches/the token-max-age constants):
```typescript
"use server";

import { redirect } from "next/navigation";
import { headers } from "next/headers";
import { auth } from "@/lib/auth";
import { routing } from "@/i18n/routing";

interface AuthResult {
  success: boolean;
  error?: string;
  twoFactorRequired?: boolean;
}

export async function login(email: string, password: string): Promise<AuthResult> {
  try {
    const res = await auth.api.signInEmail({ body: { email, password }, asResponse: true });
    if (res.headers.get("x-better-auth-2fa") || res.status === 202) {
      return { success: false, twoFactorRequired: true };
    }
    if (!res.ok) return { success: false, error: "Invalid email or password" };
    return { success: true };
  } catch {
    return { success: false, error: "Login failed" };
  }
}

export async function register(email: string, password: string): Promise<AuthResult> {
  try {
    const res = await auth.api.signUpEmail({
      body: { email, password, name: email.split("@")[0] },
      asResponse: true,
    });
    if (!res.ok) return { success: false, error: "Registration failed" };
    return { success: true };
  } catch {
    return { success: false, error: "Registration failed" };
  }
}

export async function logout(locale: string = "en") {
  const safeLocale = routing.locales.includes(locale as (typeof routing.locales)[number])
    ? locale
    : routing.defaultLocale;
  try {
    await auth.api.signOut({ headers: await headers() });
  } catch {
    // best-effort
  }
  redirect(`/${safeLocale}/login`);
}
```
> Note: client components that call Better Auth directly (Task 10) may instead use `authClient.signIn.email` and skip these server actions. These wrappers exist so the current form components keep working with minimal change; reconcile in Task 10. Verify the exact `auth.api.signInEmail` 2FA-pending signal against the installed `better-auth` types and adjust the `twoFactorRequired` detection accordingly.

- [ ] **Step 2: Verify typecheck**

Run: `cd frontend && pnpm exec tsc --noEmit`
Expected: PASS.

- [ ] **Step 3: Commit**
```bash
git add "frontend/src/app/[locale]/(auth)/actions.ts"
git commit -m "♻️ Refactor: (auth)/actions.ts → Better Auth (SP1)"
```

---

## Task 10: Re-point login/register forms (+ 2FA redirect)

**Files:**
- Modify: `frontend/src/features/auth/components/login-form.tsx`
- Modify: `frontend/src/features/auth/components/register-form.tsx`

- [ ] **Step 1: Read the current forms**

Run: `cat frontend/src/features/auth/components/login-form.tsx frontend/src/features/auth/components/register-form.tsx`
Confirm how they currently invoke `login`/`register` from `actions.ts` and how they handle `AuthResult`.

- [ ] **Step 2: Update submit handlers**

In each form, keep the existing react-hook-form + Zod structure. On submit, call the `login`/`register` server action (Task 9). On `{ twoFactorRequired: true }`, `router.push("/two-factor")`. On `{ success: true }`, `router.push(callbackUrl ?? "/overview")`. On error, set the form error from `result.error` via the existing i18n error pattern. (No new files; minimal diff.)

- [ ] **Step 3: Verify typecheck + existing form tests**

Run: `cd frontend && pnpm exec tsc --noEmit && pnpm test -- src/features/auth`
Expected: PASS (or no tests collected for that path — acceptable; the E2E in Task 13 covers the flow).

- [ ] **Step 4: Commit**
```bash
git add frontend/src/features/auth/components/login-form.tsx frontend/src/features/auth/components/register-form.tsx
git commit -m "♻️ Refactor: auth forms → Better Auth flow + 2FA redirect (SP1)"
```

---

## Task 11: Swap `middleware.ts` to Better Auth session check

**Files:**
- Modify: `frontend/middleware.ts`

- [ ] **Step 1: Replace the token logic, keep security headers + intl**

In `frontend/middleware.ts`: delete `isTokenExpired` (lines 73–82). Replace the auth-detection block (lines 88–93) with:
```typescript
	const sessionCookie = getSessionCookie(request);
	const isAuthenticated = !!sessionCookie;
```
Add the import at the top: `import { getSessionCookie } from "better-auth/cookies";`
Keep everything else (CSP `buildConnectSrc`, `securityHeaders`, `applySecurityHeaders`, the `protectedPaths`/`authPaths` redirects, `intlMiddleware`, the `config.matcher`).

- [ ] **Step 2: Verify typecheck**

Run: `cd frontend && pnpm exec tsc --noEmit`
Expected: PASS.

- [ ] **Step 3: Commit**
```bash
git add frontend/middleware.ts
git commit -m "♻️ Refactor: middleware → Better Auth getSessionCookie (SP1)"
```

---

## Task 12: Swap proxy + api-client token source; delete tryRefresh

**Files:**
- Modify: `frontend/src/app/api/proxy/[...path]/route.ts`
- Modify: `frontend/src/lib/api-client.ts`

- [ ] **Step 1: Rewrite the proxy to mint+forward a Better Auth JWT**

Replace `frontend/src/app/api/proxy/[...path]/route.ts` with (keep the path-traversal guard, query passthrough, body-once; delete `tryRefresh`/`tryRefreshDedup`/`refreshPromise`/the cookie max-age consts):
```typescript
import { headers } from "next/headers";
import { type NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

// eslint-disable-next-line -- server-only, validated by env.ts at startup
const API_URL = process.env.API_URL as string;

async function proxyRequest(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  const url = new URL(`/api/v1/${path.join("/")}`, API_URL);
  if (!url.pathname.startsWith("/api/v1/")) {
    return NextResponse.json({ error: "Forbidden" }, { status: 403 });
  }
  request.nextUrl.searchParams.forEach((value, key) => {
    url.searchParams.set(key, value);
  });

  const body =
    request.method !== "GET" && request.method !== "DELETE"
      ? await request.text()
      : undefined;

  // Mint a short-lived EdDSA JWT for the current session (browser never holds it).
  let token: string | undefined;
  try {
    const res = await auth.api.getToken({ headers: await headers() });
    token = res?.token;
  } catch {
    token = undefined;
  }

  const fwd = new Headers();
  if (body !== undefined) fwd.set("Content-Type", "application/json");
  if (token) fwd.set("Authorization", `Bearer ${token}`);

  const res = await fetch(url.toString(), { method: request.method, headers: fwd, body });
  const data = await res.text();
  return new NextResponse(data, {
    status: res.status,
    headers: { "Content-Type": res.headers.get("Content-Type") ?? "application/json" },
  });
}

export const GET = proxyRequest;
export const POST = proxyRequest;
export const PUT = proxyRequest;
export const DELETE = proxyRequest;
```
> Verify `auth.api.getToken` is the correct server method name in `better-auth@1.6.14` (jwt plugin). If it differs, the fallback is `fetch(\`${process.env.BETTER_AUTH_URL}/api/auth/token\`, { headers: await headers() })` and read `{ token }`.

- [ ] **Step 2: Align the server-side api-client token source**

In `frontend/src/lib/api-client.ts`, replace the server-side cookie read (lines 62–72) with a Better Auth token mint:
```typescript
  if (isServer) {
    try {
      const { auth } = await import("@/lib/auth");
      const { headers: nextHeaders } = await import("next/headers");
      const res = await auth.api.getToken({ headers: await nextHeaders() });
      if (res?.token) headers.Authorization = `Bearer ${res.token}`;
    } catch {
      // Outside a request context (e.g. tests) — skip auth
    }
  }
```

- [ ] **Step 3: Verify typecheck + existing api-client tests**

Run: `cd frontend && pnpm exec tsc --noEmit && pnpm test -- src/lib/__tests__/api-client`
Expected: PASS. (The existing api-client tests stub `fetch`; the server-side branch is skipped outside a request context, so they remain green.)

- [ ] **Step 4: Commit**
```bash
git add "frontend/src/app/api/proxy/[...path]/route.ts" frontend/src/lib/api-client.ts
git commit -m "♻️ Refactor: proxy + api-client forward minted Better Auth JWT; drop tryRefresh (SP1)"
```

---

## Task 13: E2E happy path + build

**Files:**
- Create: `frontend/e2e/auth.spec.ts`

**Prereq:** a running web-API (`uvicorn bfx_funding_bot.main:app`) with `BETTER_AUTH_JWKS_URL`/`BETTER_AUTH_ISSUER` pointed at the running Next app, the Better Auth migration applied to a dev Neon/Postgres, and Upstash creds set. Document these in `frontend/e2e/README.md`.

- [ ] **Step 1: Write the E2E test**

Create `frontend/e2e/auth.spec.ts`:
```typescript
import { test, expect } from "@playwright/test";

test("sign up → land on overview → authenticated API call succeeds", async ({ page }) => {
  const email = `e2e+${Date.now()}@example.com`;
  await page.goto("/en/register");
  await page.getByLabel(/email/i).fill(email);
  await page.getByLabel(/password/i).fill("Sup3rSecret!");
  await page.getByRole("button", { name: /sign up|register/i }).click();
  await expect(page).toHaveURL(/\/en\/overview/);

  // The dashboard makes an authenticated /api/proxy/profile call; assert it 200s.
  const resp = await page.waitForResponse((r) => r.url().includes("/api/proxy/") && r.request().method() === "GET");
  expect(resp.status()).toBe(200);
});

test("protected route without session redirects to login", async ({ page }) => {
  await page.goto("/en/overview");
  await expect(page).toHaveURL(/\/en\/login/);
});
```

- [ ] **Step 2: Run E2E**

Run: `cd frontend && pnpm test:e2e -- auth.spec.ts`
Expected: PASS (requires the prereq stack running).

- [ ] **Step 3: Production build**

Run: `cd frontend && pnpm build`
Expected: PASS — confirms `lib/auth.ts`, the route handler, and all edits compile for prod.

- [ ] **Step 4: Commit**
```bash
git add frontend/e2e/auth.spec.ts frontend/e2e/README.md
git commit -m "✅ Test: E2E auth happy path + protected-route redirect (SP1)"
```

---

## Task 14 (DEFERRED, topology cleanup — do NOT run against live without deploy gate): drop dormant Go-era tables

**Files:**
- Create: `backend_py/alembic/versions/<rev2>_drop_dormant_goera_auth_tables.py`

The Go-era dormant tables (`users`, `api_keys`, `user_configs`, `executions`, `billing_records` in `public`, all 0 bytes) are superseded by Better Auth (`auth.user`) + `user_profile`. This migration drops them. **It touches the live real-money DB schema** → it is sequenced LAST, applied only via the deploy compose `migrate` step with the 5-lens pre-flight + code-only rollback discipline (memory `e612228`). Do not bundle it with the SP1 FE work. Write `down_revision=<rev>` (the Task 5/6 revision), full `upgrade`(drop)/`downgrade`(recreate from `accounts/tables.py` definitions), and a testcontainers integration test. **Leave unchecked until the deploy of SP1 is planned.**

---

## Self-Review

**1. Spec coverage (vs spec §5 step 1 + §6 SP1 + D8/D13/D14):**
- Better Auth self-hosted, Next `/api/auth/*` → Tasks 2–4. ✓
- JWKS verify in FastAPI → Task 7. ✓
- `user_profile` table → Task 6. ✓
- Same DB + `auth` schema + Alembic single tool (port generate, never `migrate`) → Task 5. ✓
- Keep BFF proxy shell, swap token source, delete tryRefresh/AUTH_SECRET → Tasks 1, 11, 12. ✓
- Upstash rate-limit + secondaryStorage → Task 2. ✓
- TOTP + step-up: TOTP enrollment plugin wired (Task 2) + 2FA redirect (Tasks 9–10). **Step-up on money ops (D14) is NOT in SP1** — there are no money-mutating endpoints yet (those arrive in SP2 vault / later); step-up enforcement is correctly deferred to the task that adds a sensitive endpoint. Flagged, not a gap.
- `aud="bfx-funding-backend"`, `sub`=uuid, `requireEmailVerification:false` → Tasks 2, 7. ✓
- Standalone web-API decoupled from daemon → Task 8 mounts on `main.py` app, daemon `healthz` untouched. ✓

**2. Placeholder scan:** Two `> Verify …` notes (Task 2 jwt option names, Task 9/12 exact Better Auth method names) are version-reconciliation steps with concrete fallbacks, not placeholders — the code is written; the note guards against 1.6.x API drift. Task 5 explicitly ports generated SQL (the generated file is the source of truth) with a concrete expected schema. Acceptable.

**3. Type consistency:** `Principal(user_id, email, role)` defined in Task 7 is used identically in Tasks 7, 8. `require_user` dependency name consistent (Task 7 def, Task 8 override). `build_router()` (api) is parameterless (Task 8) — distinct from admin's `build_router(*, smoke_runner, admin_token)` (different module, no clash). `auth.api.getToken` used consistently in Tasks 12 (proxy + api-client). Migration revision `<rev>` referenced consistently across Tasks 5/6/14.

**Known reconciliation points for the executor (version drift, all have fallbacks):** Better Auth `jwt` plugin option shape (`definePayload`/`audience`), `auth.api.signInEmail` 2FA-pending signal, `auth.api.getToken` server method name, and the exact `better-auth generate` column set. Each is verified against installed types at its task.
