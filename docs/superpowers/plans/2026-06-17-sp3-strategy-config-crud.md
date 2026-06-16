# SP3 — Strategy Config CRUD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the existing `UserConfig` table to a thin, inert `/api/v1/configs` CRUD on the standalone web-API so the frontend strategy page works end-to-end.

**Architecture:** Mirror the SP2 vault layering — Pydantic DTOs (`schemas.py`) → service module (`config_service.py`) → router (`config.py`) → `main.py` wiring → hand-crafted Alembic migration. The daemon is not touched; the config is stored but never read by the lending engine (SP6 territory).

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async, Pydantic v2, Alembic, pytest + sqlite (unit), `uv`.

**Spec:** `docs/superpowers/specs/2026-06-16-sp3-strategy-config-crud-design.md`

**Conventions (from CLAUDE.md):**
- All backend commands run from `backend_py/`: `cd backend_py && uv run …` (root dir hits the wrong pyenv Python).
- Gate before any commit: `cd backend_py && uv run pytest -m "not integration"` green + `uv run ruff check` + `uv run mypy src/`.
- Commit messages use emoji prefixes (`✨ Feat:`, `✅ Test:`, `📝 Docs:`, …).
- `alembic upgrade/check/--autogenerate` is **forbidden locally** (the `.env` symlink points at live Neon). Migrations are hand-crafted and applied on the VM at deploy.

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `backend_py/src/bfx_funding_bot/modules/api/schemas.py` | Add `Range`, `StrategyConfigBody`, `UserConfigResponse` DTOs | Modify |
| `backend_py/src/bfx_funding_bot/modules/accounts/tables.py` | Fix `UserConfig` model (TEXT user_id, drop model FK, client uuid default) | Modify |
| `backend_py/src/bfx_funding_bot/modules/accounts/config_service.py` | `get/upsert/delete` over `user_configs` | Create |
| `backend_py/src/bfx_funding_bot/modules/api/config.py` | `GET/PUT/DELETE /api/v1/configs` router | Create |
| `backend_py/src/bfx_funding_bot/main.py` | Include the config router | Modify |
| `backend_py/alembic/versions/a2b3c4d5e6f7_add_user_configs.py` | Create `public.user_configs` | Create |
| `backend_py/tests/test_config_schemas.py` | DTO validation/serialization tests | Create |
| `backend_py/tests/test_config_service.py` | Service round-trip tests (sqlite) | Create |
| `backend_py/tests/test_config_router.py` | Endpoint tests (TestClient) | Create |
| `backend_py/tests/test_user_configs_migration.py` | Migration revision-chain guard | Create |

---

## Task 1: Config DTOs (schemas)

Pure validation/serialization — no DB. Mirrors the existing `schemas.py` style.

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/api/schemas.py`
- Test: `backend_py/tests/test_config_schemas.py`

- [ ] **Step 1: Write the failing tests**

Create `backend_py/tests/test_config_schemas.py`:

```python
import pytest
from pydantic import ValidationError

from bfx_funding_bot.modules.api.schemas import (
    StrategyConfigBody,
    UserConfigResponse,
)

_VALID = {
    "currency": "USD",
    "amount": {"min": 50, "max": 10000},
    "rate": {"min": 0.0001, "max": 0.001},
    "period": {"min": 2, "max": 30},
    "autoRenew": True,
}


def test_valid_body_parses_and_dumps_camelcase():
    body = StrategyConfigBody.model_validate(_VALID)
    dumped = body.model_dump(by_alias=True)
    assert dumped["currency"] == "USD"
    assert dumped["amount"] == {"min": 50.0, "max": 10000.0}
    assert dumped["autoRenew"] is True


def test_negative_amount_rejected():
    bad = {**_VALID, "amount": {"min": -1, "max": 10}}
    with pytest.raises(ValidationError):
        StrategyConfigBody.model_validate(bad)


def test_min_greater_than_max_rejected():
    bad = {**_VALID, "rate": {"min": 0.002, "max": 0.001}}
    with pytest.raises(ValidationError):
        StrategyConfigBody.model_validate(bad)


def test_empty_currency_rejected():
    bad = {**_VALID, "currency": ""}
    with pytest.raises(ValidationError):
        StrategyConfigBody.model_validate(bad)


def test_response_serializes_camelcase():
    resp = UserConfigResponse(
        id="11111111-1111-1111-1111-111111111111",
        user_id="user_abc",
        config=_VALID,
        created_at="2026-06-17T00:00:00+00:00",
        updated_at="2026-06-17T00:00:00+00:00",
    ).model_dump(by_alias=True)
    assert resp["userId"] == "user_abc"
    assert resp["createdAt"] == "2026-06-17T00:00:00+00:00"
    assert resp["config"]["autoRenew"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/test_config_schemas.py -v`
Expected: FAIL — `ImportError: cannot import name 'StrategyConfigBody'`.

- [ ] **Step 3: Add the DTOs**

Append to `backend_py/src/bfx_funding_bot/modules/api/schemas.py`:

```python
from pydantic import model_validator


class Range(BaseModel):
    """A {min, max} bound. min/max must be positive and min <= max. Used for
    amount, rate (daily ratio) and period. Modelled as float to keep zero
    divergence from the FE's single rangeSchema (which types all three as
    number); period values are integers by convention (FE step=1)."""

    min: float = Field(gt=0)
    max: float = Field(gt=0)

    @model_validator(mode="after")
    def _min_le_max(self) -> "Range":
        if self.min > self.max:
            raise ValueError("min must be <= max")
        return self


class StrategyConfigBody(BaseModel):
    """Validated PUT /configs body. Mirrors the FE Zod strategyConfigSchema
    (positive ranges, min<=max, non-empty currency). Stored verbatim as the
    user_configs.config JSONB blob. The backend is unit-agnostic on rate; it
    stores the daily ratio the FE sends and never converts."""

    model_config = ConfigDict(populate_by_name=True)

    currency: str = Field(min_length=1)
    amount: Range
    rate: Range
    period: Range
    auto_renew: bool = Field(alias="autoRenew")


class UserConfigResponse(BaseModel):
    id: str
    user_id: str = Field(serialization_alias="userId")
    config: dict[str, object]
    created_at: str = Field(serialization_alias="createdAt")
    updated_at: str = Field(serialization_alias="updatedAt")
```

Note: `BaseModel`, `ConfigDict`, `Field` are already imported at the top of `schemas.py`; add the `model_validator` import shown above to that same `from pydantic import …` line rather than a second import.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/test_config_schemas.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Lint + commit**

```bash
cd backend_py && uv run ruff check && uv run mypy src/bfx_funding_bot/modules/api/schemas.py
git add backend_py/src/bfx_funding_bot/modules/api/schemas.py backend_py/tests/test_config_schemas.py
git commit -m "✨ Feat: SP3 config DTOs (StrategyConfigBody/Range/UserConfigResponse)"
```

---

## Task 2: Fix the `UserConfig` ORM model

Bring the pre-SP1 `UserConfig` into line with the Better Auth identity model (TEXT user_id, no model FK, client uuid default), so it round-trips in sqlite.

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/accounts/tables.py` (the `UserConfig` class)
- Test: `backend_py/tests/test_user_config_model.py`

- [ ] **Step 1: Write the failing test**

Create `backend_py/tests/test_user_config_model.py`:

```python
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.accounts.tables  # noqa: F401 (registers models)
import bfx_funding_bot.modules.accounts.user_profile  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.tables import UserConfig


@pytest_asyncio.fixture
async def session(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.mark.asyncio
async def test_userconfig_inserts_with_text_user_id_and_autogen_id(session):
    row = UserConfig(user_id="user_abc", config={"currency": "USD"})
    session.add(row)
    await session.flush()
    await session.refresh(row)
    assert isinstance(row.id, uuid.UUID)        # client-side default fired
    assert row.user_id == "user_abc"            # TEXT, not coerced to UUID
    assert row.config == {"currency": "USD"}
    assert row.created_at is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/test_user_config_model.py -v`
Expected: FAIL — the current model has no client-side `default=uuid4`, so `row.id` is `None` after flush on sqlite (assertion fails or PK NULL error); user_id is declared `UUID`.

- [ ] **Step 3: Replace the `UserConfig` class**

In `backend_py/src/bfx_funding_bot/modules/accounts/tables.py`, replace the entire existing `UserConfig` class with:

```python
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
    config: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    __table_args__ = (Index("idx_user_configs_user_id", "user_id", unique=True),)
```

All referenced names (`UUID`, `uuid4`, `PG_UUID`, `Text`, `JSON`, `JSONB`, `DateTime`, `func`, `text`, `Index`, `Any`, `Mapped`, `mapped_column`) are already imported at the top of `tables.py`. If `ForeignKey` becomes unused after this change, leave it — `Execution`/`BillingRecord` still reference it.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/test_user_config_model.py -v`
Expected: PASS.

Also confirm no regression in the existing model/table suite:
Run: `cd backend_py && uv run pytest tests/test_api_keys_router.py -q`
Expected: PASS (create_all still works with the changed model).

- [ ] **Step 5: Lint + commit**

```bash
cd backend_py && uv run ruff check && uv run mypy src/bfx_funding_bot/modules/accounts/tables.py
git add backend_py/src/bfx_funding_bot/modules/accounts/tables.py backend_py/tests/test_user_config_model.py
git commit -m "🐛 Fix: SP3 UserConfig ORM — TEXT user_id, drop model FK, client uuid default"
```

---

## Task 3: Config service (`config_service.py`)

`get` / `upsert` / `delete` over `user_configs`. Mirrors `vault.py` (no HTTP concerns; `ensure_user_profile` before insert).

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/accounts/config_service.py`
- Test: `backend_py/tests/test_config_service.py`

- [ ] **Step 1: Write the failing tests**

Create `backend_py/tests/test_config_service.py`:

```python
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.accounts.tables  # noqa: F401
import bfx_funding_bot.modules.accounts.user_profile  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts import config_service
from bfx_funding_bot.modules.accounts.tables import UserConfig

_CFG = {"currency": "USD", "amount": {"min": 50, "max": 10000}, "autoRenew": True}


@pytest_asyncio.fixture
async def session(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.mark.asyncio
async def test_get_absent_returns_none(session):
    assert await config_service.get_user_config(session, user_id="nobody") is None


@pytest.mark.asyncio
async def test_upsert_creates_then_get_returns_it(session):
    row = await config_service.upsert_user_config(session, user_id="user_abc", config=_CFG)
    assert row.user_id == "user_abc"
    assert row.config == _CFG
    fetched = await config_service.get_user_config(session, user_id="user_abc")
    assert fetched is not None
    assert fetched.id == row.id


@pytest.mark.asyncio
async def test_upsert_twice_updates_in_place(session):
    first = await config_service.upsert_user_config(session, user_id="user_abc", config=_CFG)
    new_cfg = {**_CFG, "currency": "UST"}
    second = await config_service.upsert_user_config(session, user_id="user_abc", config=new_cfg)
    assert second.id == first.id                     # same row, not a new insert
    assert second.config["currency"] == "UST"
    count = await session.scalar(select(func.count()).select_from(UserConfig))
    assert count == 1                                # single-config-per-user


@pytest.mark.asyncio
async def test_delete_returns_true_then_false(session):
    await config_service.upsert_user_config(session, user_id="user_abc", config=_CFG)
    assert await config_service.delete_user_config(session, user_id="user_abc") is True
    assert await config_service.delete_user_config(session, user_id="user_abc") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/test_config_service.py -v`
Expected: FAIL — `ModuleNotFoundError: …config_service`.

- [ ] **Step 3: Write the service**

Create `backend_py/src/bfx_funding_bot/modules/accounts/config_service.py`:

```python
"""SP3 config service: get/upsert/delete the user's strategy-preference config.
No HTTP concerns — the config router maps these to endpoints/status codes.
INERT: the lending daemon never reads user_configs; this is FE-facing storage
and SP6 groundwork."""
from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.accounts.provisioning import ensure_user_profile
from bfx_funding_bot.modules.accounts.tables import UserConfig


async def get_user_config(session: AsyncSession, *, user_id: str) -> UserConfig | None:
    return await session.scalar(select(UserConfig).where(UserConfig.user_id == user_id))


async def upsert_user_config(
    session: AsyncSession, *, user_id: str, config: dict[str, Any]
) -> UserConfig:
    """Create the user's config row, or overwrite it if one exists (one row per
    user). ensure_user_profile first so the DB-level FK to user_profiles is
    satisfied on insert. The idx_user_configs_user_id UNIQUE is the race backstop
    for a rare concurrent double-insert (single operator in v1 -> no contention)."""
    await ensure_user_profile(session, user_id=user_id)
    row = await session.scalar(select(UserConfig).where(UserConfig.user_id == user_id))
    if row is None:
        row = UserConfig(user_id=user_id, config=config)
        session.add(row)
    else:
        row.config = config
    await session.flush()
    await session.refresh(row)
    return row


async def delete_user_config(session: AsyncSession, *, user_id: str) -> bool:
    result = await session.execute(
        delete(UserConfig).where(UserConfig.user_id == user_id)
    )
    return result.rowcount > 0


__all__ = ["delete_user_config", "get_user_config", "upsert_user_config"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/test_config_service.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Lint + commit**

```bash
cd backend_py && uv run ruff check && uv run mypy src/bfx_funding_bot/modules/accounts/config_service.py
git add backend_py/src/bfx_funding_bot/modules/accounts/config_service.py backend_py/tests/test_config_service.py
git commit -m "✨ Feat: SP3 config service (get/upsert/delete user_configs)"
```

---

## Task 4: Config router + main wiring

`GET/PUT/DELETE /api/v1/configs`, mirroring `api_keys.py` (require_user + get_session, `{data}` envelope, 404/204). Wire into `main.py`.

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/api/config.py`
- Modify: `backend_py/src/bfx_funding_bot/main.py`
- Test: `backend_py/tests/test_config_router.py`

- [ ] **Step 1: Write the failing tests**

Create `backend_py/tests/test_config_router.py`:

```python
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.accounts.tables  # noqa: F401
import bfx_funding_bot.modules.accounts.user_profile  # noqa: F401
from bfx_funding_bot.core.auth import Principal, require_user
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.api.config import build_config_router
from bfx_funding_bot.modules.api.deps import get_session

_VALID = {
    "currency": "USD",
    "amount": {"min": 50, "max": 10000},
    "rate": {"min": 0.0001, "max": 0.001},
    "period": {"min": 2, "max": 30},
    "autoRenew": True,
}


@pytest_asyncio.fixture
async def app_client(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)

    app = FastAPI()
    app.include_router(build_config_router())

    async def _fake_user():
        return Principal(user_id="user_abc", email="will@example.com", role="operator")

    async def _override_session():
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[require_user] = _fake_user
    app.dependency_overrides[get_session] = _override_session
    return TestClient(app)


def test_get_absent_returns_404(app_client):
    assert app_client.get("/api/v1/configs").status_code == 404


def test_put_creates_then_get_returns(app_client):
    r = app_client.put("/api/v1/configs", json=_VALID)
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["userId"] == "user_abc"
    assert body["config"]["currency"] == "USD"
    assert body["id"] and body["createdAt"]

    g = app_client.get("/api/v1/configs")
    assert g.status_code == 200
    assert g.json()["data"]["config"]["autoRenew"] is True


def test_put_twice_updates_in_place(app_client):
    first = app_client.put("/api/v1/configs", json=_VALID).json()["data"]
    second = app_client.put(
        "/api/v1/configs", json={**_VALID, "currency": "UST"}
    ).json()["data"]
    assert second["id"] == first["id"]
    assert second["config"]["currency"] == "UST"


def test_put_invalid_returns_422_or_400(app_client):
    bad = {**_VALID, "amount": {"min": -1, "max": 10}}
    assert app_client.put("/api/v1/configs", json=bad).status_code in (400, 422)


def test_put_min_gt_max_rejected(app_client):
    bad = {**_VALID, "rate": {"min": 0.002, "max": 0.001}}
    assert app_client.put("/api/v1/configs", json=bad).status_code in (400, 422)


def test_delete_existing_then_absent(app_client):
    app_client.put("/api/v1/configs", json=_VALID)
    assert app_client.delete("/api/v1/configs").status_code == 204
    assert app_client.delete("/api/v1/configs").status_code == 404


def test_requires_auth():
    app = FastAPI()
    app.include_router(build_config_router())
    c = TestClient(app)
    assert c.get("/api/v1/configs").status_code in (401, 403)
    assert c.put("/api/v1/configs", json=_VALID).status_code in (401, 403)
    assert c.delete("/api/v1/configs").status_code in (401, 403)
```

Note: FastAPI returns **422** for request-body validation failures by default, so the invalid-body tests accept `400` or `422`. The spec's "400" is the conceptual contract; the FE treats any non-2xx on save as an error, so 422 is acceptable.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/test_config_router.py -v`
Expected: FAIL — `ModuleNotFoundError: …api.config`.

- [ ] **Step 3: Write the router**

Create `backend_py/src/bfx_funding_bot/modules/api/config.py`:

```python
"""SP3 configs endpoints. Thin: maps config_service results to HTTP. Every route
is gated by require_user and scoped to principal.user_id. {"data": ...} envelope.
INERT: stored config is never read by the lending daemon (SP6 will consume it)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.auth import Principal, require_user
from bfx_funding_bot.modules.accounts import config_service
from bfx_funding_bot.modules.accounts.tables import UserConfig
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.schemas import StrategyConfigBody, UserConfigResponse


def _to_response(row: UserConfig) -> dict[str, object]:
    return UserConfigResponse(
        id=str(row.id),
        user_id=row.user_id,
        config=dict(row.config),
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    ).model_dump(by_alias=True)


def build_config_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["configs"])

    @router.get("/configs")
    async def get_config(
        user: Principal = Depends(require_user),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        row = await config_service.get_user_config(session, user_id=user.user_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
        return {"data": _to_response(row)}

    @router.put("/configs")
    async def put_config(
        body: StrategyConfigBody,
        user: Principal = Depends(require_user),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        row = await config_service.upsert_user_config(
            session, user_id=user.user_id, config=body.model_dump(by_alias=True)
        )
        return {"data": _to_response(row)}

    @router.delete("/configs", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_config(
        user: Principal = Depends(require_user),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> Response:
        deleted = await config_service.delete_user_config(session, user_id=user.user_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
```

- [ ] **Step 4: Wire the router into `main.py`**

In `backend_py/src/bfx_funding_bot/main.py`, add the import next to the other router imports:

```python
from bfx_funding_bot.modules.api.config import build_config_router
```

and add the include next to the other `include_router` calls:

```python
app.include_router(build_config_router())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/test_config_router.py -v`
Expected: PASS (7 passed).

- [ ] **Step 6: Lint + commit**

```bash
cd backend_py && uv run ruff check && uv run mypy src/bfx_funding_bot/modules/api/config.py src/bfx_funding_bot/main.py
git add backend_py/src/bfx_funding_bot/modules/api/config.py backend_py/src/bfx_funding_bot/main.py backend_py/tests/test_config_router.py
git commit -m "✨ Feat: SP3 /api/v1/configs router (GET/PUT/DELETE) + main wiring"
```

---

## Task 5: Migration for `public.user_configs`

Hand-crafted (no autogenerate against live Neon). Mirrors the SP2 `f1a2b3c4d5e6` shape.

**Files:**
- Create: `backend_py/alembic/versions/a2b3c4d5e6f7_add_user_configs.py`
- Test: `backend_py/tests/test_user_configs_migration.py`

- [ ] **Step 1: Write the failing revision-chain guard test**

Create `backend_py/tests/test_user_configs_migration.py`:

```python
import importlib


def test_user_configs_migration_chains_off_sp2_head():
    mod = importlib.import_module(
        "alembic.versions.a2b3c4d5e6f7_add_user_configs"
    )
    assert mod.revision == "a2b3c4d5e6f7"
    assert mod.down_revision == "f1a2b3c4d5e6"  # SP2 api_key_vault (prior head)
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/test_user_configs_migration.py -v`
Expected: FAIL — `ModuleNotFoundError` (migration file does not exist yet).

- [ ] **Step 3: Write the migration**

Create `backend_py/alembic/versions/a2b3c4d5e6f7_add_user_configs.py`:

```python
"""add user_configs (SP3)

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-06-17

Creates public.user_configs (one inert strategy-preference config per user) with
a DB-level FK to user_profiles.user_id (which carries uq_user_profiles_user_id).
Hand-crafted (live Neon: no autogenerate); applied on the VM via the migrate
compose service. GRANT to bfx_webapi is a separate psql runbook step.
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlalchemy.dialects.postgresql

from alembic import op

revision: str = "a2b3c4d5e6f7"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_configs",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column(
            "config", sa.dialects.postgresql.JSONB(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user_profiles.user_id"],
            ondelete="CASCADE", name="fk_user_configs_user_profile",
        ),
        schema="public",
    )
    op.create_index(
        "idx_user_configs_user_id", "user_configs", ["user_id"],
        unique=True, schema="public",
    )


def downgrade() -> None:
    op.drop_index("idx_user_configs_user_id", table_name="user_configs", schema="public")
    op.drop_table("user_configs", schema="public")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/test_user_configs_migration.py -v`
Expected: PASS.

Do **NOT** run `alembic upgrade`/`check` locally — the `.env` symlink targets live Neon. The migration is applied on the VM at deploy (Task: deploy runbook below).

- [ ] **Step 5: Full gate + commit**

```bash
cd backend_py && uv run pytest -m "not integration" -q && uv run ruff check && uv run mypy src/
git add backend_py/alembic/versions/a2b3c4d5e6f7_add_user_configs.py backend_py/tests/test_user_configs_migration.py
git commit -m "✨ Feat: SP3 user_configs migration (off head f1a2b3c4d5e6)"
```

---

## Deploy runbook (manual, after merge — not a code task)

Mirror the SP2 deploy steps. Will runs these (Claude has no prod access without an interactive session):

1. Push to origin (`gh auth switch Will413028` first, else "Repository not found").
2. On the VM, apply the migration via the migrate compose service: `cd backend_py && uv run alembic upgrade head` (runs against live Neon from the VM). Expect head → `a2b3c4d5e6f7`.
3. `GRANT SELECT, INSERT, UPDATE, DELETE ON public.user_configs TO bfx_webapi;` via psql (same runbook slot as SP2's api_keys grant).
4. Redeploy the webapi compose service (`--no-deps` so the daemon is untouched, byte-identical).
5. Smoke: an authenticated `PUT /api/v1/configs` → 200, `GET` → 200, `DELETE` → 204 from the live FE strategy page (or curl with a minted JWT).

---

## Self-Review (completed by author)

**Spec coverage:**
- §3 contract (request/response, status codes) → Tasks 1 (DTOs) + 4 (router).
- §4.1 ORM fix → Task 2. §4.2 migration → Task 5. §4.3 service → Task 3. §4.4 schemas → Task 1. §4.5 router → Task 4. §4.6 main wiring → Task 4.
- §5 validation (mirror-FE: positive + min≤max + non-empty currency) → Task 1 tests + 4 invalid-body tests.
- §6 test matrix (GET 404/200, PUT create/update, PUT invalid, DELETE 204/404, 401, single-config-per-user) → Tasks 3 + 4. Single-config-per-user is covered by `test_upsert_twice_updates_in_place` (service) and `test_put_twice_updates_in_place` (router).
- GRANT/deploy note → deploy runbook section.

**Placeholder scan:** none — every step has runnable code/commands.

**Type consistency:** `StrategyConfigBody` / `Range` / `UserConfigResponse` (Task 1) are referenced consistently in Tasks 3–4. `config_service.{get,upsert,delete}_user_config` signatures match between Task 3 definition and Task 4 calls. `build_config_router` name consistent across Task 4 + tests. The `config` dict round-trips as `dict[str, object]` in the response DTO and `dict[str, Any]` in the model/service — compatible.

**Known deviation from spec:** FastAPI emits **422** (not 400) for body-validation failures; the router tests accept `400|422`. Documented in Task 4 Step 1. Acceptable: the FE treats any non-2xx save as an error.
