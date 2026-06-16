# SP3 — Strategy Config CRUD (`/api/v1/configs`)

**Status:** approved (brainstorming) → ready for writing-plans
**Date:** 2026-06-16
**Depends on:** SP1 (auth/JWKS) + SP2 (vault), both deployed live on the VM canary.
**Parent spec:** `docs/superpowers/specs/2026-06-06-frontend-saas-architecture-design.md` (SP3 row, L160/L188)

## 1. Summary

Wire the already-defined `UserConfig` ORM table to a thin `/api/v1/configs` CRUD
on the standalone web-API, mirroring the SP2 vault layering (service / router /
schema / migration). The endpoint **persists the user's strategy-preference
config and nothing more** — the lending daemon never reads this table.

Two goals:
1. Un-break the existing frontend strategy page (`(dashboard)/strategy`), whose
   `useConfig` / `useSaveConfig` / `useResetConfig` hooks already call
   `GET/PUT/DELETE /configs` and currently hit a missing endpoint.
2. Lay clean groundwork for SP6 (customer knob-write that actually drives a
   per-tenant engine).

## 2. Scope & boundary

**In scope**
- `/api/v1/configs` resource: `GET` / `PUT` (upsert) / `DELETE`.
- `user_configs` table migration + bringing the `UserConfig` ORM model in line
  with the post-SP1 (Better Auth) identity model.
- Pydantic request/response DTOs + a `config_service.py` module + a `config.py`
  router + `main.py` wiring.
- pytest unit coverage.

**Explicitly out of scope**
- **Engine consumption.** The config is *inert*: `cells.yaml` remains the
  daemon's only config source, loaded once at startup, not hot-loadable. The
  daemon code is **not touched**; zero impact on the live canary. Engine
  consumption of per-tenant config is SP6.
- **Operator live read surface** (what the daemon is actually running:
  cells / standing-quotes / effective caps). That is SP4's `ops/*` projections,
  not SP3.
- **Frontend changes.** The strategy page is already wired to this contract. The
  SaaS frontend reshape (`(ops)` route group, flag-hiding customer pages,
  polling) is separate frontend work, not part of SP3.
- **Stale Go-era tables.** `User` (self-hosted auth, `users`), `Execution`,
  `BillingRecord` in `accounts/tables.py` are pre-rebuild leftovers. They are
  not migrated and SP3 leaves them untouched.

## 3. The contract (fixed by the existing frontend)

Because SP3 must un-break the FE *without changing the FE*, the FE is the
de-facto contract. The backend must match it exactly.

**Request body — `PUT /api/v1/configs`** (camelCase, nested; `rate` is a **daily
rate ratio**, already converted client-side from APY by `÷365÷100`):

```json
{
  "currency": "USD",
  "amount":  { "min": 50,     "max": 10000 },
  "rate":    { "min": 0.0001, "max": 0.001 },
  "period":  { "min": 2,      "max": 30 },
  "autoRenew": true
}
```

**Response — `GET` / `PUT`** (api-client auto-unwraps the `{ "data": … }`
envelope into `UserConfig`):

```json
{
  "data": {
    "id": "<uuid>",
    "userId": "<text>",
    "config": { "currency": "USD", "amount": {…}, "rate": {…}, "period": {…}, "autoRenew": true },
    "createdAt": "<iso8601>",
    "updatedAt": "<iso8601>"
  }
}
```

**Status codes**
- `GET` with a config → `200` + `{data: UserConfig}`.
- `GET` with no config → `404` `not_found` (the FE catches 404 → `null`, a normal empty state).
- `PUT` valid → `200` + `{data: UserConfig}` (creates on first save, updates in place thereafter — one config per user).
- `PUT` invalid body → `400` with field-level detail.
- `PUT` / `GET` / `DELETE` with no/invalid JWT → `401`.
- `DELETE` with a config → `204` (no body).
- `DELETE` with no config → `404` `not_found`.

Note: backend is **unit-agnostic on rate** — it stores whatever the FE sends
(the daily ratio). All APY↔ratio conversion stays in the FE
(`strategy-form.tsx`). The backend never converts.

## 4. Components

### 4.1 ORM correctness fix — `UserConfig` (`modules/accounts/tables.py`)

The current `UserConfig` model is a pre-SP1 (Go-era) artifact: `user_id` is
`UUID` with a model-level `ForeignKey("users.id")` pointing at the **old
self-hosted `users` table**. After SP1 the real identity is Better Auth's
`auth.user.id`, which is **TEXT**, and the Principal carried on the JWT is a TEXT
string. Bring `UserConfig` in line with the SP2 pattern (`UserProfile` /
`APIKey`):

- `user_id`: `Text` (not `PG_UUID`), matching `auth.user.id` / `Principal.user_id`.
- **Remove the model-level `ForeignKey`.** The FK is enforced at the DB level by
  the migration only (mirrors `UserProfile`/`APIKey`): a model-level FK to a
  table outside this ORM's `Base.metadata` breaks `Base.metadata.create_all()`
  in sqlite test fixtures.
- `id`: add `default=uuid4` (client-side) alongside the existing
  `server_default=gen_random_uuid()` — sqlite unit tests have no
  `gen_random_uuid()`.
- `created_at` / `updated_at`: align to `server_default=func.current_timestamp()`
  (test-portable, matches SP2).
- `config`: keep the existing `JSONB`-on-Postgres / `JSON`-on-sqlite variant.

### 4.2 Migration (`alembic/versions/a2b3c4d5e6f7_add_user_configs.py`)

Hand-crafted (live Neon → no autogenerate), revision id `a2b3c4d5e6f7`
(suggested; finalize at implementation), `down_revision = "f1a2b3c4d5e6"`
(current head). Mirror the SP2 migration shape:

- `op.create_table("user_configs", schema="public", …)`:
  - `id` `UUID` PK, `server_default=gen_random_uuid()`.
  - `user_id` `Text` NOT NULL.
  - `config` `JSONB` NOT NULL.
  - `created_at` / `updated_at` `timestamptz` NOT NULL `server_default=now()`.
  - `ForeignKeyConstraint(["user_id"], ["user_profiles.user_id"], ondelete="CASCADE", name="fk_user_configs_user_profile")`.
- `op.create_index("idx_user_configs_user_id", "user_configs", ["user_id"], unique=True, schema="public")`.
- `downgrade()`: drop index, drop table.

`user_profiles.user_id` already carries the `uq_user_profiles_user_id` UNIQUE
constraint (added by f1a2b3c4d5e6), which is a valid FK target.

Deploy/runbook note: after `alembic upgrade head` on the VM, `GRANT` on
`public.user_configs` to `bfx_webapi` (same psql runbook step as SP2). Document
in the plan; not executed by the migration.

### 4.3 Service — `modules/accounts/config_service.py` (mirror `vault.py`)

No HTTP concerns. Async functions over `AsyncSession`:

- `get_user_config(session, *, user_id: str) -> UserConfig | None`
- `upsert_user_config(session, *, user_id: str, config: dict) -> UserConfig`
  - `await ensure_user_profile(session, user_id=user_id)` first (FK requires the
    profile row; same as `vault.create_api_key`).
  - read-modify-write: if a row exists, overwrite `config` (and let
    `updated_at` refresh); else insert. The `idx_user_configs_user_id` UNIQUE is
    the race backstop for the rare concurrent double-insert (single operator in
    v1 → effectively no contention).
- `delete_user_config(session, *, user_id: str) -> bool` (False if absent → router 404).

### 4.4 Schemas (append to `modules/api/schemas.py`)

- `Range` — `{ min: float, max: float }` with a `min <= max` model validator.
  Reused for `amount`, `rate`, `period`. Period values are conventionally
  integers (FE `step=1`) but modelled as float to keep **zero divergence** from
  the FE's single `rangeSchema` (which types all three as `number`).
- `StrategyConfigBody` — `{ currency: str (min_length 1), amount: Range,
  rate: Range, period: Range, autoRenew: bool }`, `populate_by_name=True`,
  `autoRenew` alias. This is the validated PUT body; the stored `config` JSONB is
  its `model_dump(by_alias=True)`.
- `UserConfigResponse` — `{ id: str, userId (alias), config: dict,
  createdAt (alias), updatedAt (alias) }`, camelCase serialization aliases
  (mirror `ApiKeyResponse`).

### 4.5 Router — `modules/api/config.py` (mirror `api_keys.py`)

`build_config_router()` → `APIRouter(prefix="/api/v1", tags=["configs"])`:

- `GET /configs` → load via service; `None` → `HTTPException 404 "not_found"`;
  else `{"data": _to_response(row)}`.
- `PUT /configs` → validate `StrategyConfigBody`; `upsert_user_config`; `200`
  `{"data": _to_response(row)}`.
- `DELETE /configs` → `delete_user_config`; `False` → `404 "not_found"`; else
  `Response(status_code=204)`.
- Every route: `Depends(require_user)` + `Depends(get_session)`.
- `_to_response(row)` builds `UserConfigResponse(...).model_dump(by_alias=True)`.

### 4.6 Wiring — `main.py`

`app.include_router(build_config_router())` alongside the SP1/SP2 routers.

## 5. Validation decision

Backend validation **mirrors the FE Zod** (`strategyConfigSchema`): each of
`amount`/`rate`/`period` requires `min > 0`, `max > 0`, `min <= max`; `currency`
non-empty; `autoRenew` boolean. A violation → `400` with field detail.

The openspec-era **domain bounds are deliberately NOT enforced** in v1
(`amount.min ≥ 50`, `period ∈ [2,120]`, etc.). Rationale:
- A backend stricter than the FE creates a "FE accepts, backend 400s"
  divergence footgun.
- The config is inert in v1 → low stakes for loose bounds.
- Domain bounds belong in SP6, enforced in **both** layers once the knob
  actually drives the engine.

`currency` is not enum-gated (the FE input is `disabled`, always `"USD"`).

## 6. Testing

pytest unit, sqlite, mirroring `tests/test_api_keys_router.py`
(`app.dependency_overrides` for `get_session` + `require_user`). No integration
tier (no crypto, no external calls):

- `GET` no config → 404.
- `GET` existing config → 200, body round-trips the stored config.
- `PUT` first save → 200, row created.
- `PUT` second save (same user) → 200, **updates in place** (still one row;
  single-config-per-user invariant).
- `PUT` invalid (negative number; `min > max`) → 400.
- `DELETE` existing → 204.
- `DELETE` absent → 404.
- Any verb without a valid JWT → 401.

Gate: `cd backend_py && uv run pytest -m "not integration"` green +
`ruff check` + `mypy src/`. Migration sanity: `alembic check` is **not** runnable
against live Neon — verify the migration via a sqlite/throwaway upgrade in the
test path or a local Postgres, per the plan.

## 7. Out-of-scope follow-ups (noted, not done here)

- FE "preview / not yet active" labelling on the strategy page (honest signposting
  that the knob is inert until SP6) — frontend reshape work.
- SP4 operator live read surface (`ops/cells`, standing-quotes, effective caps).
- SP6 engine consumption of per-tenant config + enforcing domain bounds in both layers.
