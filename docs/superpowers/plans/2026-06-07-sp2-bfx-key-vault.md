# SP2 — BFX-key Envelope Vault + Bitfinex Verify Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓使用者在 web-API 安全地新增 / 列出 / 刪除 / 驗證一把 Bitfinex API key — secret 經 app-managed envelope 加密 at-rest，verify 走 fail-closed permission 檢查（funding write=on 且 withdraw=off）。

**Architecture:** 明文 secret 由 Next Server Action 直送 FastAPI web-API（不走 client proxy），FastAPI 用 `core/crypto.py`（AES-256-GCM envelope：per-record DEK，KEK 在 web-API env）加密後存 `api_keys` 表（FK `user_profiles.user_id`，DB-level only）。verify 解密 → 呼叫 Bitfinex `/v2/auth/r/permissions` → 判定。每個 endpoint `require_user` + scope by `principal.user_id`。

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy 2.0 async / Alembic / `cryptography` (AESGCM) / httpx / pytest + testcontainers；FE = Next 16 Server Action + TanStack Query。

**Spec:** `docs/superpowers/specs/2026-06-07-sp2-bfx-key-vault-design.md`

**指令目錄**：所有 `pytest` / `mypy` / `ruff` / `alembic` 在 `backend_py/`（`cd backend_py && uv run …`）。FE 在 `frontend/`（`pnpm …`）。

---

## File Structure

**Backend（新增）**
- `backend_py/src/bfx_funding_bot/core/crypto.py` — envelope encrypt/decrypt（純函式，kek 由參數傳入）+ `load_kek()` + `VaultNotConfiguredError`。
- `backend_py/src/bfx_funding_bot/modules/accounts/provisioning.py` — `ensure_user_profile()`（JIT，SP3+ 共用）。
- `backend_py/src/bfx_funding_bot/modules/accounts/vault.py` — vault service（create/list/delete/verify orchestration；無 HTTP）。
- `backend_py/src/bfx_funding_bot/modules/api/deps.py` — `get_session` + `get_bitfinex_auth_rest` FastAPI deps。
- `backend_py/src/bfx_funding_bot/modules/api/schemas.py` — Pydantic request/response（camelCase alias）。
- `backend_py/src/bfx_funding_bot/modules/api/api_keys.py` — api-keys router（薄 wiring）。
- `backend_py/alembic/versions/f1a2b3c4d5e6_add_api_key_vault.py` — 手刻 migration。

**Backend（修改）**
- `backend_py/pyproject.toml` — 加 `cryptography` dep。
- `backend_py/src/bfx_funding_bot/core/settings.py` — 加 `bfx_vault_kek` 欄。
- `backend_py/src/bfx_funding_bot/modules/accounts/tables.py` — 改造 `APIKey` model（envelope 欄位、user_id Text、移除 model-level FK）。
- `backend_py/src/bfx_funding_bot/main.py` — include api_keys router。
- `backend_py/src/bfx_funding_bot/external/bitfinex/auth_rest.py` — 加 `KeyPermissions` + `parse_key_permissions` + `get_key_permissions`。

**Backend（測試新增）**
- `tests/test_crypto.py`、`tests/external/bitfinex/test_auth_rest_permissions.py`、`tests/test_provisioning.py`、`tests/test_vault_service.py`、`tests/test_api_keys_router.py`、`tests/integration/test_api_key_vault_migration.py`。

**Frontend（修改 / 新增）**
- `frontend/src/app/[locale]/(dashboard)/api-keys/actions.ts`（新）— `createApiKeyAction`。
- `frontend/src/features/api-keys/hooks/use-api-keys.ts` — `useCreateApiKey` 改呼 action。
- `frontend/src/features/api-keys/hooks/__tests__/create-action.test.ts`（新）— Vitest。

**Deploy**
- `scripts/deploy-vm.sh` — webapi.env 加 `BFX_VAULT_KEK`。

---

## Task 1: 加 `cryptography` dep + `BFX_VAULT_KEK` setting

**Files:**
- Modify: `backend_py/pyproject.toml`（`dependencies` 陣列）
- Modify: `backend_py/src/bfx_funding_bot/core/settings.py:42-52`

- [x] **Step 1: 加 dependency**

`backend_py/pyproject.toml` 的 `dependencies` 陣列加一行（緊接現有 `"cryptography"` 不存在 → 新增；pyjwt[crypto] 雖已間接帶入，仍宣告 direct dep）：

```toml
    "cryptography>=43",
```

- [x] **Step 2: 同步環境**

Run: `cd backend_py && uv sync`
Expected: 成功，`cryptography` 已在 lock。

- [x] **Step 3: 在 settings.py 記錄 KEK env var（不加欄位）**

> **[Plan amendment 2026-06-07 — review]** 原計畫加 `bfx_vault_kek: str = ""` 欄位，但 `core.crypto.load_kek()` 是直接讀 `os.environ`（為了讓 crypto unit test 不需 DATABASE_URL），該欄位永遠不會被讀 → 是「看起來 load-bearing 實則 dead」的誤導欄位。改為只加文件化註解，不加欄位（grep 驗證全 src 無 `settings.bfx_vault_kek` reader）。

`core/settings.py` 的 `Settings` class，在 `jwt_audience` 之後加一段註解（**不**新增欄位）：

```python
    # SP2 vault: the api-key envelope KEK is the env var BFX_VAULT_KEK
    # (base64-encoded 32 bytes), read DIRECTLY from os.environ by
    # core.crypto.load_kek() — intentionally NOT a Settings field, so crypto
    # unit tests don't require DATABASE_URL (which Settings() needs via the
    # .env symlink). Deploy presence is enforced by deploy-vm.sh preflight (Task 12).
```

- [x] **Step 4: 確認 import 與型別乾淨**

Run: `cd backend_py && uv run python -c "import cryptography; from bfx_funding_bot.core.settings import Settings; print('ok')"`
Expected: 印 `ok`（注意：此步在 `backend_py/` 下，`.env` symlink 提供 `DATABASE_URL`）。

- [x] **Step 5: Commit**

```bash
git add backend_py/pyproject.toml backend_py/uv.lock backend_py/src/bfx_funding_bot/core/settings.py
git commit -m "🔧 Chore: add cryptography dep + document BFX_VAULT_KEK env (SP2)"
```

---

## Task 2: `core/crypto.py` — envelope encrypt/decrypt

**Files:**
- Create: `backend_py/src/bfx_funding_bot/core/crypto.py`
- Test: `backend_py/tests/test_crypto.py`

- [x] **Step 1: 寫失敗測試**

`tests/test_crypto.py`：

```python
import base64

import pytest

from bfx_funding_bot.core.crypto import (
    Envelope,
    VaultNotConfiguredError,
    decrypt_secret,
    encrypt_secret,
    load_kek,
)

_KEK = base64.b64encode(bytes(range(32))).decode()  # deterministic 32-byte KEK


def test_round_trip():
    kek = base64.b64decode(_KEK)
    env = encrypt_secret("super-secret", user_id="user_abc", kek=kek)
    assert isinstance(env, Envelope)
    assert env.secret_ciphertext != b"super-secret"
    assert env.key_version == 1
    assert decrypt_secret(env, user_id="user_abc", kek=kek) == "super-secret"


def test_nonce_is_unique_per_call():
    kek = base64.b64decode(_KEK)
    a = encrypt_secret("x", user_id="u", kek=kek)
    b = encrypt_secret("x", user_id="u", kek=kek)
    assert a.secret_nonce != b.secret_nonce
    assert a.secret_ciphertext != b.secret_ciphertext


def test_tampered_ciphertext_fails():
    kek = base64.b64decode(_KEK)
    env = encrypt_secret("x", user_id="u", kek=kek)
    bad = Envelope(
        secret_ciphertext=env.secret_ciphertext[:-1] + bytes([env.secret_ciphertext[-1] ^ 0x01]),
        secret_nonce=env.secret_nonce,
        wrapped_dek=env.wrapped_dek,
        dek_nonce=env.dek_nonce,
        key_version=env.key_version,
    )
    with pytest.raises(Exception):  # cryptography.exceptions.InvalidTag
        decrypt_secret(bad, user_id="u", kek=kek)


def test_wrong_user_aad_fails():
    kek = base64.b64decode(_KEK)
    env = encrypt_secret("x", user_id="alice", kek=kek)
    with pytest.raises(Exception):
        decrypt_secret(env, user_id="bob", kek=kek)


def test_wrong_kek_fails():
    kek = base64.b64decode(_KEK)
    other = bytes([b ^ 0xFF for b in kek])
    env = encrypt_secret("x", user_id="u", kek=kek)
    with pytest.raises(Exception):
        decrypt_secret(env, user_id="u", kek=other)


def test_load_kek_missing_raises(monkeypatch):
    monkeypatch.delenv("BFX_VAULT_KEK", raising=False)
    with pytest.raises(VaultNotConfiguredError):
        load_kek()


def test_load_kek_wrong_length_raises(monkeypatch):
    monkeypatch.setenv("BFX_VAULT_KEK", base64.b64encode(b"too-short").decode())
    with pytest.raises(VaultNotConfiguredError):
        load_kek()


def test_load_kek_ok(monkeypatch):
    monkeypatch.setenv("BFX_VAULT_KEK", _KEK)
    assert len(load_kek()) == 32
```

- [x] **Step 2: 跑測試確認失敗**

Run: `cd backend_py && uv run pytest tests/test_crypto.py -v`
Expected: FAIL — `ModuleNotFoundError: bfx_funding_bot.core.crypto`。

- [x] **Step 3: 實作**

`core/crypto.py`：

```python
"""SP2 vault crypto: app-managed envelope encryption for Bitfinex api secrets.

Per-record DEK (AES-256-GCM) wrapped by an env-held KEK. AAD binds the row to
its user_id so a ciphertext can't be replayed under another user. Pure
functions take the KEK as an argument (testable without env / DATABASE_URL);
load_kek() reads BFX_VAULT_KEK from the process env at call time.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12
_DEK_LEN = 32
_CURRENT_KEY_VERSION = 1


class VaultNotConfiguredError(Exception):
    """BFX_VAULT_KEK missing or malformed."""


@dataclass(frozen=True, slots=True)
class Envelope:
    secret_ciphertext: bytes
    secret_nonce: bytes
    wrapped_dek: bytes
    dek_nonce: bytes
    key_version: int


def load_kek() -> bytes:
    raw = os.environ.get("BFX_VAULT_KEK", "")
    if not raw:
        raise VaultNotConfiguredError("BFX_VAULT_KEK not set")
    try:
        kek = base64.b64decode(raw, validate=True)
    except Exception as e:  # noqa: BLE001
        raise VaultNotConfiguredError("BFX_VAULT_KEK is not valid base64") from e
    if len(kek) != 32:
        raise VaultNotConfiguredError(f"BFX_VAULT_KEK must decode to 32 bytes, got {len(kek)}")
    return kek


def encrypt_secret(plaintext: str, *, user_id: str, kek: bytes) -> Envelope:
    aad = user_id.encode("utf-8")
    dek = os.urandom(_DEK_LEN)
    secret_nonce = os.urandom(_NONCE_LEN)
    secret_ct = AESGCM(dek).encrypt(secret_nonce, plaintext.encode("utf-8"), aad)
    dek_nonce = os.urandom(_NONCE_LEN)
    wrapped_dek = AESGCM(kek).encrypt(dek_nonce, dek, aad)
    return Envelope(
        secret_ciphertext=secret_ct,
        secret_nonce=secret_nonce,
        wrapped_dek=wrapped_dek,
        dek_nonce=dek_nonce,
        key_version=_CURRENT_KEY_VERSION,
    )


def decrypt_secret(env: Envelope, *, user_id: str, kek: bytes) -> str:
    aad = user_id.encode("utf-8")
    dek = AESGCM(kek).decrypt(env.dek_nonce, env.wrapped_dek, aad)
    plaintext = AESGCM(dek).decrypt(env.secret_nonce, env.secret_ciphertext, aad)
    return plaintext.decode("utf-8")
```

- [x] **Step 4: 跑測試確認通過**

Run: `cd backend_py && uv run pytest tests/test_crypto.py -v`
Expected: PASS（8 passed）。

- [x] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/core/crypto.py backend_py/tests/test_crypto.py
git commit -m "✨ Feat: envelope encryption module for vault secrets (SP2)"
```

---

## Task 3: 改造 `APIKey` model（envelope 欄位）+ UserProfile 跨方言 default

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/accounts/tables.py:46-72`
- Modify: `backend_py/src/bfx_funding_bot/modules/accounts/user_profile.py`（跨方言 default，見 Step 3b）
- Test: `backend_py/tests/test_api_key_model.py`

- [x] **Step 1: 寫失敗測試（sqlite roundtrip + unique）**

`tests/test_api_key_model.py`：

```python
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

import bfx_funding_bot.modules.accounts.tables  # noqa: F401  register APIKey
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.tables import APIKey


@pytest_asyncio.fixture
async def session(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as s:
        yield s


def _row(user_id="u1") -> APIKey:
    return APIKey(
        user_id=user_id, label="main", api_key="PUBKEY",
        secret_ciphertext=b"\x01\x02", secret_nonce=b"\x03",
        wrapped_dek=b"\x04", dek_nonce=b"\x05", key_version=1,
    )


@pytest.mark.asyncio
async def test_insert_and_read_back(session: AsyncSession):
    session.add(_row())
    await session.commit()
    got = await session.scalar(select(APIKey).where(APIKey.user_id == "u1"))
    assert got is not None
    assert got.exchange_status == "unverified"  # server default
    assert got.secret_ciphertext == b"\x01\x02"
    assert got.verified_at is None


@pytest.mark.asyncio
async def test_unique_per_user(session: AsyncSession):
    session.add(_row())
    await session.commit()
    session.add(_row())  # second key for same user
    with pytest.raises(IntegrityError):
        await session.commit()
```

- [x] **Step 2: 跑測試確認失敗**

Run: `cd backend_py && uv run pytest tests/test_api_key_model.py -v`
Expected: FAIL — `TypeError`（`APIKey` 無 `secret_ciphertext` 等欄位）。

- [x] **Step 3: 改 model**

`modules/accounts/tables.py` 把 `class APIKey`（行 46-72）整段替換為（移除舊 `users.id` FK 與單一 `api_secret`，改 envelope 欄位；`user_id` 改 `Text`，FK 只在 migration 層）：

```python
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

    __table_args__ = (Index("idx_api_keys_user_id", "user_id", unique=True),)
```

> **[Plan amendment 2026-06-07 — review, sqlite cross-dialect]** 原計畫用 `server_default=text("now()")` + `id` 只有 `server_default=text("gen_random_uuid()")`。但 Task 3/Task 10 的測試跑在 **sqlite**，sqlite 無 `now()` / `gen_random_uuid()` 函式 → INSERT 省略這些欄位時會炸 `unknown function`（已實測證實）。修法（沿用本 repo 既定跨方言慣例，見 `execution/event_store/tables.py` 的 `_NOW = func.current_timestamp()`）：
> - `id` 加 client-side `default=uuid4`（ORM INSERT 時供值，sqlite 不評估 `gen_random_uuid()`；PG 行為等價，無 DDL drift），保留 `server_default` 給 raw-SQL/migration。
> - `created_at`/`updated_at` 用 `func.current_timestamp()`（PG→`now()`、sqlite→`CURRENT_TIMESTAMP`，ANSI，兩方言皆可）。
> - **migration（Task 4）維持 `sa.text("now()")` 即可**（只跑 PG/testcontainers，不經 sqlite；migration test 不比對 server_default 文字，且本 plan 不跑 `alembic check`）。

注意 import：檔案頂部 import 已含 `Integer`、`LargeBinary`、`Text`、`DateTime`、`Index`、`text`，但 **需新增 `func`**（`from sqlalchemy import func`，或併入既有 `from sqlalchemy import ...`）與 **`uuid4`**（`from uuid import uuid4`；檔案可能已 import `UUID` type，確認 `uuid4` 也在）。`ForeignKey` 仍被其他 model 使用，勿移除 import。`from __future__ import annotations`：本檔頂部目前**沒有**（行 1 是 `from datetime import datetime`），但 `Mapped[datetime | None]` 在 Python 3.13 的 mapped_column annotation 下 OK（既有 dormant model 已用同寫法），故**不需**加 future import；勿順手改動以免污染其他 model。

- [x] **Step 3b: 同步修 `UserProfile` 跨方言 default（Task 10 router 測試在 sqlite 插 UserProfile 需要）**

> **[Plan amendment 2026-06-07 — review]** Task 10 的 router 測試走真實 create 流程 → `ensure_user_profile` 在 **sqlite** 插入 `UserProfile`。`UserProfile`（SP1 表）的 `id` 用 `server_default=text("gen_random_uuid()")`、`created_at`/`updated_at` 用 `text("now()")` → sqlite 一樣炸。此為同一 latent bug，必須一併修（不修 Task 10 sqlite 測試過不了）。

`modules/accounts/user_profile.py`：把 `id` 加 client-side `default=uuid4`（保留 `server_default`），`created_at`/`updated_at` 的 `server_default=text("now()")` 改為 `func.current_timestamp()`。新增 import `from sqlalchemy import func` 與 `from uuid import uuid4`（若尚無）。

**安全性（對 live prod 無影響）**：`default=uuid4` 是 client-side（不改 DDL，不影響既有 prod 表的 DB default `gen_random_uuid()`）；timestamp model server_default 只影響 `create_all`（測試），ORM INSERT 省略欄位時用的是 **DB 既有 default**（`now()`，migration 建的，未變）→ prod insert 行為零變化。不需 migration、不需 `alembic`。

UserProfile 的 `__table_args__` 仍宣告 unique `Index`（SP2 migration 會把它換成 unique constraint）——此 model/DB 細微 drift 無害（本 plan 不 autogenerate/check），**不動**。

驗證：UserProfile 的 sqlite 插入路徑由 Task 10 的 router 測試覆蓋（Task 6 `test_provisioning` 走 PG，不覆蓋 sqlite）。

- [x] **Step 4: 跑測試確認通過**

Run: `cd backend_py && uv run pytest tests/test_api_key_model.py -v`
Expected: PASS（2 passed）。

- [x] **Step 5: 全測試 + lint 不回歸**

Run: `cd backend_py && uv run pytest -m "not integration" -q && uv run ruff check && uv run mypy src/`
Expected: 全綠（既有測試不因 model 改動而壞 — 確認無其他程式讀舊 `api_secret` 欄位；若有編譯/型別錯誤在此修）。

- [x] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/accounts/tables.py backend_py/src/bfx_funding_bot/modules/accounts/user_profile.py backend_py/tests/test_api_key_model.py
git commit -m "♻️ Refactor: APIKey envelope vault columns + cross-dialect defaults (SP2)"
```

---

## Task 4: 手刻 migration + integration migration test

**Files:**
- Create: `backend_py/alembic/versions/f1a2b3c4d5e6_add_api_key_vault.py`
- Test: `backend_py/tests/integration/test_api_key_vault_migration.py`

- [x] **Step 1: 寫 migration**

`alembic/versions/f1a2b3c4d5e6_add_api_key_vault.py`：

```python
"""add api_key vault (SP2)

Revision ID: f1a2b3c4d5e6
Revises: e61f3d1ed7ca
Create Date: 2026-06-07

Creates public.api_keys (envelope-encrypted Bitfinex secrets) with a DB-level FK
to user_profiles.user_id. PG requires the FK target to be a UNIQUE CONSTRAINT
(a bare UNIQUE INDEX is insufficient), so this migration replaces SP1's
idx_user_profiles_user_id unique index with a uq_user_profiles_user_id unique
constraint. Hand-crafted (live Neon: no autogenerate); applied on the VM via the
migrate compose service. GRANTs to bfx_webapi are a separate psql runbook step.
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlalchemy.dialects.postgresql

from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "e61f3d1ed7ca"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # FK target must be a unique constraint, not just a unique index.
    op.drop_index(
        "idx_user_profiles_user_id", table_name="user_profiles", schema="public"
    )
    op.create_unique_constraint(
        "uq_user_profiles_user_id", "user_profiles", ["user_id"], schema="public"
    )

    op.create_table(
        "api_keys",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("api_key", sa.Text(), nullable=False),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("secret_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("dek_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "exchange_status", sa.Text(), nullable=False,
            server_default=sa.text("'unverified'"),
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verify_error", sa.Text(), nullable=True),
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
            ondelete="CASCADE", name="fk_api_keys_user_profile",
        ),
        schema="public",
    )
    op.create_index(
        "idx_api_keys_user_id", "api_keys", ["user_id"], unique=True, schema="public"
    )


def downgrade() -> None:
    op.drop_index("idx_api_keys_user_id", table_name="api_keys", schema="public")
    op.drop_table("api_keys", schema="public")
    op.drop_constraint(
        "uq_user_profiles_user_id", "user_profiles", schema="public", type_="unique"
    )
    op.create_index(
        "idx_user_profiles_user_id", "user_profiles", ["user_id"],
        unique=True, schema="public",
    )
```

- [x] **Step 2: 寫 integration 測試**

`tests/integration/test_api_key_vault_migration.py`（沿用 SP1 migration test 的 schema-reset + alembic-to-head pattern）：

```python
import pytest

from tests.integration.test_migration_per_symbol_pk import _ALEMBIC_INI

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_api_keys_table_and_fk_after_upgrade(pg_engine, monkeypatch) -> None:
    """Real alembic upgrade to head: assert public.api_keys exists, has the unique
    index, and a CASCADE FK to user_profiles.user_id. Do NOT import vault/profile
    models here (would trigger create_all before the migration runs)."""
    sync_url = pg_engine.url.render_as_string(hide_password=False).replace(
        "+asyncpg", "+psycopg")
    monkeypatch.setenv("DATABASE_URL", sync_url)

    from sqlalchemy import create_engine, inspect

    eng = create_engine(sync_url)
    try:
        with eng.begin() as setup_conn:
            setup_conn.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
            setup_conn.exec_driver_sql("DROP SCHEMA public CASCADE")
            setup_conn.exec_driver_sql("CREATE SCHEMA public")
    finally:
        eng.dispose()

    from alembic.config import Config

    from alembic import command
    command.upgrade(Config(str(_ALEMBIC_INI)), "head")

    verify_eng = create_engine(sync_url)
    try:
        with verify_eng.connect() as conn:
            insp = inspect(conn)
            tables = set(insp.get_table_names(schema="public"))
            fks = insp.get_foreign_keys("api_keys", schema="public")
            indexes = insp.get_indexes("api_keys", schema="public")
            uniques = insp.get_unique_constraints("user_profiles", schema="public")
    finally:
        verify_eng.dispose()

    assert "api_keys" in tables, f"public has {tables}"
    referring = [
        fk for fk in fks
        if fk.get("referred_table") == "user_profiles"
        and fk.get("constrained_columns") == ["user_id"]
    ]
    assert referring, f"api_keys has no FK to user_profiles; fks={fks}"
    assert referring[0].get("options", {}).get("ondelete", "").upper() == "CASCADE"
    assert any(
        idx["column_names"] == ["user_id"] and idx["unique"] for idx in indexes
    ), f"missing unique index on api_keys.user_id; indexes={indexes}"
    assert any(
        uc["column_names"] == ["user_id"] for uc in uniques
    ), f"user_profiles missing unique constraint on user_id; uniques={uniques}"
```

- [x] **Step 3: 跑 migration 測試**

Run: `cd backend_py && uv run pytest tests/integration/test_api_key_vault_migration.py -v -m integration`
Expected: PASS（需 Docker；testcontainers 起 PG，跑真 alembic upgrade head）。

- [x] **Step 4: 確認 SP1 migration test 仍綠（沒被 index→constraint 改動破壞）**

Run: `cd backend_py && uv run pytest tests/integration/test_user_profile_migration.py -v -m integration`
Expected: PASS。

- [x] **Step 5: Commit**

```bash
git add backend_py/alembic/versions/f1a2b3c4d5e6_add_api_key_vault.py backend_py/tests/integration/test_api_key_vault_migration.py
git commit -m "✨ Feat: api_key vault migration + FK to user_profiles (SP2)"
```

---

## Task 5: Bitfinex `get_key_permissions`

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/external/bitfinex/auth_rest.py`（加 dataclass + parser + method）
- Test: `backend_py/tests/external/bitfinex/test_auth_rest_permissions.py`

- [x] **Step 1: 寫失敗測試**

`tests/external/bitfinex/test_auth_rest_permissions.py`（鏡像 `test_auth_rest_wallets.py` 的 MockTransport pattern）：

```python
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import (
    BitfinexAuthREST,
    KeyPermissions,
    parse_key_permissions,
)
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials

# Bitfinex /v2/auth/r/permissions row: [scope, read(0/1), write(0/1)]
_PERMS = [
    ["account", 1, 0],
    ["orders", 0, 0],
    ["funding", 1, 1],
    ["wallets", 1, 0],
    ["withdraw", 0, 0],
]


def _ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="KEY", api_secret="SECRET"),
        allocation_cap_usdt=Decimal("0"),
    )


def test_parse_key_permissions_builds_scope_map():
    perms = parse_key_permissions(_PERMS)
    assert isinstance(perms, KeyPermissions)
    assert perms.can("funding", write=True) is True
    assert perms.can("withdraw", write=True) is False
    assert perms.can("account", write=False) is True
    assert perms.can("nonexistent", write=True) is False


def test_parse_rejects_non_list():
    with pytest.raises(BitfinexShapeError):
        parse_key_permissions({"not": "list"})


def test_parse_rejects_short_row():
    with pytest.raises(BitfinexShapeError):
        parse_key_permissions([["funding", 1]])


@pytest.mark.asyncio
async def test_get_key_permissions_signs_and_parses():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=_PERMS)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, nonce_provider=lambda: 1)
        perms = await client.get_key_permissions(ctx=_ctx())

    assert perms.can("funding", write=True) is True
    assert captured["url"] == "https://api.bitfinex.com/v2/auth/r/permissions"
    assert captured["headers"]["bfx-apikey"] == "KEY"
    assert "bfx-signature" in captured["headers"]


@pytest.mark.asyncio
async def test_get_key_permissions_raises_on_http_error():
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, nonce_provider=lambda: 1)
        with pytest.raises(BitfinexAPIError):
            await client.get_key_permissions(ctx=_ctx())
```

- [x] **Step 2: 跑測試確認失敗**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_auth_rest_permissions.py -v`
Expected: FAIL — `ImportError`（`KeyPermissions` / `parse_key_permissions` 未定義）。

- [x] **Step 3: 實作**

`auth_rest.py`：在 `_WALLETS_PATH` 常數附近加 path 常數，並加 dataclass + parser + method。

(a) path 常數（行 ~141 後）：

```python
_PERMISSIONS_PATH = "v2/auth/r/permissions"  # no body args; sign_request prepends /api/
```

(b) dataclass + parser（放在 `parse_wallets` 之後）：

```python
@dataclass(frozen=True, slots=True)
class KeyPermissions:
    """scope -> (read, write). Bitfinex /v2/auth/r/permissions rows are
    [scope, read(0/1), write(0/1)]."""

    scopes: dict[str, tuple[bool, bool]]

    def can(self, scope: str, *, write: bool) -> bool:
        read_flag, write_flag = self.scopes.get(scope, (False, False))
        return write_flag if write else read_flag


def parse_key_permissions(raw: Any) -> KeyPermissions:
    if not isinstance(raw, list):
        raise BitfinexShapeError(
            f"expected list of permission rows, got {type(raw).__name__}: {raw!r}"
        )
    scopes: dict[str, tuple[bool, bool]] = {}
    for row in raw:
        if not isinstance(row, list) or len(row) < 3:
            raise BitfinexShapeError(f"permission row malformed: {row!r}")
        scopes[str(row[0])] = (bool(int(row[1])), bool(int(row[2])))
    return KeyPermissions(scopes=scopes)
```

(c) method on `BitfinexAuthREST`（放在 `get_funding_available` 之後）：

```python
    async def get_key_permissions(self, *, ctx: AccountContext) -> KeyPermissions:
        """POST /v2/auth/r/permissions (signed). Returns the key's scope→(read,write)
        map. Same error contract as the other auth reads: BitfinexAPIError on
        transport/HTTP error (status_code=0 for transport), BitfinexShapeError on
        invalid JSON / shape."""
        path = _PERMISSIONS_PATH
        body_bytes = json.dumps({}).encode("utf-8")
        nonce = self._nonce_provider()
        headers = sign_request(
            body=body_bytes, nonce=nonce,
            api_secret=ctx.credentials.api_secret, path=path,
        )
        headers["bfx-apikey"] = ctx.credentials.api_key
        headers["Content-Type"] = "application/json"
        try:
            resp = await self._http.post(
                f"{self._base_url}/{path}", content=body_bytes, headers=headers,
                timeout=30.0,
            )
        except httpx.HTTPError as e:
            raise BitfinexAPIError(status_code=0, message=f"transport error: {e}", raw=None) from e
        if resp.status_code >= 400:
            raise BitfinexAPIError(
                status_code=resp.status_code,
                message=resp.reason_phrase or "http error", raw=resp.text,
            )
        try:
            raw = resp.json()
        except json.JSONDecodeError as e:
            raise BitfinexShapeError(f"invalid JSON in permissions response: {e}") from e
        return parse_key_permissions(raw)
```

- [x] **Step 4: 跑測試確認通過**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_auth_rest_permissions.py -v`
Expected: PASS（5 passed）。

- [x] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/external/bitfinex/auth_rest.py backend_py/tests/external/bitfinex/test_auth_rest_permissions.py
git commit -m "✨ Feat: Bitfinex get_key_permissions for SP2 verify (SP2)"
```

---

## Task 6: `ensure_user_profile`（JIT provision）

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/accounts/provisioning.py`
- Test: `backend_py/tests/test_provisioning.py`

- [ ] **Step 1: 寫失敗測試（PG，需 user_profiles 表）**

`tests/test_provisioning.py`：

```python
import bfx_funding_bot.modules.accounts.user_profile  # noqa: F401  register UserProfile
import pytest
from sqlalchemy import func, select

from bfx_funding_bot.core.db import session_scope
from bfx_funding_bot.modules.accounts.provisioning import ensure_user_profile
from bfx_funding_bot.modules.accounts.user_profile import UserProfile

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_creates_profile_when_absent(pg_session_factory):
    async with session_scope(pg_session_factory) as s:
        await ensure_user_profile(s, user_id="user_jit")
    async with session_scope(pg_session_factory) as s:
        count = await s.scalar(
            select(func.count()).select_from(UserProfile).where(UserProfile.user_id == "user_jit")
        )
    assert count == 1


@pytest.mark.asyncio
async def test_idempotent(pg_session_factory):
    async with session_scope(pg_session_factory) as s:
        await ensure_user_profile(s, user_id="user_dup")
    async with session_scope(pg_session_factory) as s:
        await ensure_user_profile(s, user_id="user_dup")
    async with session_scope(pg_session_factory) as s:
        count = await s.scalar(
            select(func.count()).select_from(UserProfile).where(UserProfile.user_id == "user_dup")
        )
    assert count == 1
```

> **Note for executor:** 此 module-level `import ...user_profile` 會把 `UserProfile` 註冊進 `Base.metadata`，session-scoped `pg_engine` fixture 的 `create_all` 因此建出 `user_profiles`（model 無 FK，安全）。migration 測試自己 DROP SCHEMA + 跑 alembic，不受影響。

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd backend_py && uv run pytest tests/test_provisioning.py -v -m integration`
Expected: FAIL — `ModuleNotFoundError: ...accounts.provisioning`。

- [ ] **Step 3: 實作**

`modules/accounts/provisioning.py`：

```python
"""JIT provisioning of the app-side user_profiles row. Shared by SP2 vault and
later SP3+ endpoints: any authenticated write ensures the principal's profile
exists before inserting owned rows.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.accounts.user_profile import UserProfile


async def ensure_user_profile(session: AsyncSession, *, user_id: str, plan: str = "free") -> None:
    """Insert a user_profiles row for user_id if absent. Idempotent; flushes so
    a subsequent FK insert in the same transaction sees the row."""
    existing = await session.scalar(
        select(UserProfile.id).where(UserProfile.user_id == user_id)
    )
    if existing is None:
        session.add(UserProfile(user_id=user_id, plan=plan))
        await session.flush()
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd backend_py && uv run pytest tests/test_provisioning.py -v -m integration`
Expected: PASS（2 passed）。

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/accounts/provisioning.py backend_py/tests/test_provisioning.py
git commit -m "✨ Feat: JIT ensure_user_profile helper (SP2)"
```

---

## Task 7: vault service — create / list / delete

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/accounts/vault.py`
- Test: `backend_py/tests/test_vault_service.py`（本 task 寫 create/list/delete 部分）

- [ ] **Step 1: 寫失敗測試**

`tests/test_vault_service.py`：

```python
import base64

import bfx_funding_bot.modules.accounts.user_profile  # noqa: F401
import pytest
from sqlalchemy import select

from bfx_funding_bot.core.crypto import decrypt_secret
from bfx_funding_bot.core.db import session_scope
from bfx_funding_bot.modules.accounts.tables import APIKey
from bfx_funding_bot.modules.accounts.vault import (
    Envelope,
    KeyAlreadyExists,
    create_api_key,
    delete_api_key,
    list_api_keys,
)

pytestmark = pytest.mark.integration

_KEK = base64.b64decode(base64.b64encode(bytes(range(32))))


@pytest.mark.asyncio
async def test_create_encrypts_and_provisions(pg_session_factory):
    async with session_scope(pg_session_factory) as s:
        row = await create_api_key(
            s, user_id="u_create", label="main",
            api_key="PUB", api_secret="my-secret", kek=_KEK,
        )
        rid = row.id
    async with session_scope(pg_session_factory) as s:
        got = await s.scalar(select(APIKey).where(APIKey.id == rid))
        assert got is not None
        assert got.api_key == "PUB"
        assert got.exchange_status == "unverified"
        # secret is recoverable only via envelope
        env = Envelope(
            secret_ciphertext=got.secret_ciphertext, secret_nonce=got.secret_nonce,
            wrapped_dek=got.wrapped_dek, dek_nonce=got.dek_nonce, key_version=got.key_version,
        )
        assert decrypt_secret(env, user_id="u_create", kek=_KEK) == "my-secret"


@pytest.mark.asyncio
async def test_create_duplicate_raises(pg_session_factory):
    async with session_scope(pg_session_factory) as s:
        await create_api_key(s, user_id="u_dup", label="a", api_key="P", api_secret="s", kek=_KEK)
    async with session_scope(pg_session_factory) as s:
        with pytest.raises(KeyAlreadyExists):
            await create_api_key(s, user_id="u_dup", label="b", api_key="P2", api_secret="s2", kek=_KEK)


@pytest.mark.asyncio
async def test_list_scoped_to_user(pg_session_factory):
    async with session_scope(pg_session_factory) as s:
        await create_api_key(s, user_id="u_list_a", label="a", api_key="P", api_secret="s", kek=_KEK)
        await create_api_key(s, user_id="u_list_b", label="b", api_key="P", api_secret="s", kek=_KEK)
    async with session_scope(pg_session_factory) as s:
        rows = await list_api_keys(s, user_id="u_list_a")
    assert len(rows) == 1
    assert rows[0].user_id == "u_list_a"


@pytest.mark.asyncio
async def test_delete_returns_false_for_other_user(pg_session_factory):
    async with session_scope(pg_session_factory) as s:
        row = await create_api_key(s, user_id="u_del", label="a", api_key="P", api_secret="s", kek=_KEK)
        rid = row.id
    async with session_scope(pg_session_factory) as s:
        assert await delete_api_key(s, user_id="someone_else", key_id=rid) is False
    async with session_scope(pg_session_factory) as s:
        assert await delete_api_key(s, user_id="u_del", key_id=rid) is True
    async with session_scope(pg_session_factory) as s:
        assert await list_api_keys(s, user_id="u_del") == []
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd backend_py && uv run pytest tests/test_vault_service.py -v -m integration`
Expected: FAIL — `ModuleNotFoundError: ...accounts.vault`。

- [ ] **Step 3: 實作（create/list/delete；verify 在 Task 8 加）**

`modules/accounts/vault.py`：

```python
"""SP2 vault service: orchestrates the api_keys table + envelope crypto. No HTTP
concerns — the api_keys router maps these to endpoints/status codes. verify also
takes an injected Bitfinex client so it stays unit-testable."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.crypto import Envelope, decrypt_secret, encrypt_secret
from bfx_funding_bot.modules.accounts.provisioning import ensure_user_profile
from bfx_funding_bot.modules.accounts.tables import APIKey


class KeyAlreadyExists(Exception):
    """User already has a key (single-key-per-user invariant)."""


async def list_api_keys(session: AsyncSession, *, user_id: str) -> Sequence[APIKey]:
    result = await session.scalars(select(APIKey).where(APIKey.user_id == user_id))
    return list(result)


async def create_api_key(
    session: AsyncSession, *, user_id: str, label: str,
    api_key: str, api_secret: str, kek: bytes,
) -> APIKey:
    existing = await session.scalar(select(APIKey).where(APIKey.user_id == user_id))
    if existing is not None:
        raise KeyAlreadyExists()
    await ensure_user_profile(session, user_id=user_id)
    env = encrypt_secret(api_secret, user_id=user_id, kek=kek)
    row = APIKey(
        user_id=user_id, label=label, api_key=api_key,
        secret_ciphertext=env.secret_ciphertext, secret_nonce=env.secret_nonce,
        wrapped_dek=env.wrapped_dek, dek_nonce=env.dek_nonce, key_version=env.key_version,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def delete_api_key(session: AsyncSession, *, user_id: str, key_id: UUID) -> bool:
    row = await session.scalar(
        select(APIKey).where(APIKey.id == key_id, APIKey.user_id == user_id)
    )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd backend_py && uv run pytest tests/test_vault_service.py -v -m integration`
Expected: PASS（4 passed）。

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/accounts/vault.py backend_py/tests/test_vault_service.py
git commit -m "✨ Feat: vault service create/list/delete (SP2)"
```

---

## Task 8: vault service — verify（fail-closed permission 判定）

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/accounts/vault.py`（加 `verify_api_key`）
- Test: `backend_py/tests/test_vault_service.py`（append verify 測試）

- [ ] **Step 1: 加失敗測試**

在 `tests/test_vault_service.py` 末尾 append（用一個 fake permissions client，避免真網路）：

```python
from dataclasses import dataclass

from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
from bfx_funding_bot.modules.accounts.vault import verify_api_key


@dataclass
class _FakeClient:
    """Stands in for BitfinexAuthREST.get_key_permissions."""
    perms: KeyPermissions | None = None
    error: Exception | None = None

    async def get_key_permissions(self, *, ctx) -> KeyPermissions:
        if self.error is not None:
            raise self.error
        assert self.perms is not None
        return self.perms


def _perms(funding_w=True, withdraw_w=False) -> KeyPermissions:
    return KeyPermissions(scopes={
        "funding": (True, funding_w),
        "withdraw": (False, withdraw_w),
    })


async def _make_key(factory, user_id="u_v") -> "UUID":
    async with session_scope(factory) as s:
        row = await create_api_key(s, user_id=user_id, label="a", api_key="PUB", api_secret="sec", kek=_KEK)
        return row.id


@pytest.mark.asyncio
async def test_verify_success_marks_verified(pg_session_factory):
    rid = await _make_key(pg_session_factory, "u_ok")
    async with session_scope(pg_session_factory) as s:
        row = await verify_api_key(
            s, _FakeClient(perms=_perms()), user_id="u_ok", key_id=rid, kek=_KEK
        )
    assert row is not None
    assert row.exchange_status == "verified"
    assert row.verified_at is not None
    assert row.last_verify_error is None


@pytest.mark.asyncio
async def test_verify_withdraw_enabled_fails_closed(pg_session_factory):
    rid = await _make_key(pg_session_factory, "u_wd")
    async with session_scope(pg_session_factory) as s:
        row = await verify_api_key(
            s, _FakeClient(perms=_perms(withdraw_w=True)), user_id="u_wd", key_id=rid, kek=_KEK
        )
    assert row.exchange_status == "failed"
    assert row.last_verify_error == "withdraw_must_be_disabled"


@pytest.mark.asyncio
async def test_verify_no_funding_write_fails(pg_session_factory):
    rid = await _make_key(pg_session_factory, "u_nf")
    async with session_scope(pg_session_factory) as s:
        row = await verify_api_key(
            s, _FakeClient(perms=_perms(funding_w=False)), user_id="u_nf", key_id=rid, kek=_KEK
        )
    assert row.exchange_status == "failed"
    assert row.last_verify_error == "funding_write_required"


@pytest.mark.asyncio
async def test_verify_bad_credentials_fails(pg_session_factory):
    rid = await _make_key(pg_session_factory, "u_bad")
    err = BitfinexAPIError(status_code=500, message="apikey: invalid", raw=None)
    async with session_scope(pg_session_factory) as s:
        row = await verify_api_key(
            s, _FakeClient(error=err), user_id="u_bad", key_id=rid, kek=_KEK
        )
    assert row.exchange_status == "failed"
    assert row.last_verify_error == "invalid_credentials"


@pytest.mark.asyncio
async def test_verify_transport_error_reraises(pg_session_factory):
    rid = await _make_key(pg_session_factory, "u_net")
    err = BitfinexAPIError(status_code=0, message="transport error", raw=None)
    async with session_scope(pg_session_factory) as s:
        with pytest.raises(BitfinexAPIError):
            await verify_api_key(s, _FakeClient(error=err), user_id="u_net", key_id=rid, kek=_KEK)


@pytest.mark.asyncio
async def test_verify_unknown_key_returns_none(pg_session_factory):
    from uuid import uuid4
    async with session_scope(pg_session_factory) as s:
        assert await verify_api_key(
            s, _FakeClient(perms=_perms()), user_id="nobody", key_id=uuid4(), kek=_KEK
        ) is None
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd backend_py && uv run pytest tests/test_vault_service.py -k verify -v -m integration`
Expected: FAIL — `ImportError: cannot import name 'verify_api_key'`。

- [ ] **Step 3: 實作 `verify_api_key`**

在 `vault.py` 末尾加（定義一個輕量 protocol 給注入的 client，免 import 真 client 造成循環）：

```python
from typing import Protocol

from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from decimal import Decimal


class _PermissionsClient(Protocol):
    async def get_key_permissions(self, *, ctx: AccountContext) -> KeyPermissions: ...


async def verify_api_key(
    session: AsyncSession, client: _PermissionsClient, *,
    user_id: str, key_id: UUID, kek: bytes,
) -> APIKey | None:
    """Load the user's key, decrypt, check Bitfinex permissions fail-closed
    (funding write on AND withdraw off), persist the verdict. Returns None if no
    such key for this user (router -> 404). Re-raises BitfinexAPIError with
    status_code==0 (transport) so the router can map it to 502; any other
    Bitfinex API error marks the key failed/invalid_credentials."""
    row = await session.scalar(
        select(APIKey).where(APIKey.id == key_id, APIKey.user_id == user_id)
    )
    if row is None:
        return None

    secret = decrypt_secret(
        Envelope(
            secret_ciphertext=row.secret_ciphertext, secret_nonce=row.secret_nonce,
            wrapped_dek=row.wrapped_dek, dek_nonce=row.dek_nonce, key_version=row.key_version,
        ),
        user_id=user_id, kek=kek,
    )
    ctx = AccountContext(
        account_id=str(row.id),
        credentials=Credentials(api_key=row.api_key, api_secret=secret),
        allocation_cap_usdt=Decimal("0"),
    )

    try:
        perms = await client.get_key_permissions(ctx=ctx)
    except BitfinexAPIError as e:
        if e.status_code == 0:
            raise  # transport/unreachable -> router 502, status unchanged
        row.exchange_status = "failed"
        row.last_verify_error = "invalid_credentials"
        await session.flush()
        return row

    if not perms.can("funding", write=True):
        row.exchange_status = "failed"
        row.last_verify_error = "funding_write_required"
    elif perms.can("withdraw", write=True):
        row.exchange_status = "failed"
        row.last_verify_error = "withdraw_must_be_disabled"
    else:
        row.exchange_status = "verified"
        row.verified_at = datetime.now(timezone.utc)
        row.last_verify_error = None
    await session.flush()
    return row
```

> 把這些新 import 併到檔案頂部既有 import 區（勿重複；`Decimal`/`Protocol` 視情況上移）。

- [ ] **Step 4: 跑全 vault 測試確認通過**

Run: `cd backend_py && uv run pytest tests/test_vault_service.py -v -m integration`
Expected: PASS（10 passed）。

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/accounts/vault.py backend_py/tests/test_vault_service.py
git commit -m "✨ Feat: vault verify_api_key fail-closed permission check (SP2)"
```

---

## Task 9: API deps + Pydantic schemas

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/api/deps.py`
- Create: `backend_py/src/bfx_funding_bot/modules/api/schemas.py`
- Test: `backend_py/tests/test_api_schemas.py`

- [ ] **Step 1: 寫失敗測試（schema alias roundtrip）**

`tests/test_api_schemas.py`：

```python
from bfx_funding_bot.modules.api.schemas import (
    ApiKeyResponse,
    CreateApiKeyRequest,
    VerifyResultResponse,
)


def test_create_request_accepts_camelcase():
    req = CreateApiKeyRequest.model_validate(
        {"label": "main", "apiKey": "PUB", "apiSecret": "SEC"}
    )
    assert req.api_key == "PUB"
    assert req.api_secret == "SEC"


def test_response_serializes_camelcase_and_masks_secret():
    resp = ApiKeyResponse(
        id="abc", label="main", api_key="PUB",
        exchange_status="verified", created_at="2026-06-07T00:00:00Z",
        verified_at="2026-06-07T00:00:01Z",
    )
    dumped = resp.model_dump(by_alias=True)
    assert dumped["apiKey"] == "PUB"
    assert dumped["apiSecret"] == "****"
    assert dumped["exchangeStatus"] == "verified"
    assert dumped["createdAt"] == "2026-06-07T00:00:00Z"
    assert dumped["verifiedAt"] == "2026-06-07T00:00:01Z"


def test_verify_result():
    assert VerifyResultResponse(status="failed", error="withdraw_must_be_disabled").model_dump() == {
        "status": "failed", "error": "withdraw_must_be_disabled",
    }
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd backend_py && uv run pytest tests/test_api_schemas.py -v`
Expected: FAIL — `ModuleNotFoundError: ...api.schemas`。

- [ ] **Step 3: 實作 schemas**

`modules/api/schemas.py`：

```python
"""Pydantic DTOs for the api-keys endpoints. Response keys are camelCase to match
the FE types (frontend/src/types/index.ts) and the SP1 profile endpoint."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CreateApiKeyRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    label: str = ""
    api_key: str = Field(alias="apiKey")
    api_secret: str = Field(alias="apiSecret")


class ApiKeyResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    label: str
    api_key: str = Field(serialization_alias="apiKey")
    api_secret: str = Field(default="****", serialization_alias="apiSecret")
    exchange_status: str = Field(serialization_alias="exchangeStatus")
    created_at: str = Field(serialization_alias="createdAt")
    verified_at: str | None = Field(default=None, serialization_alias="verifiedAt")


class VerifyResultResponse(BaseModel):
    status: str
    error: str | None = None
```

- [ ] **Step 4: 實作 deps**

`modules/api/deps.py`：

```python
"""FastAPI dependencies for the web-API: per-request DB session bound to the
app's session_factory, and a per-request Bitfinex auth REST client. Both are
overridden in tests via app.dependency_overrides."""
from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="db_not_configured"
        )
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_bitfinex_auth_rest() -> AsyncIterator[BitfinexAuthREST]:
    async with httpx.AsyncClient() as http:
        yield BitfinexAuthREST(http=http)
```

- [ ] **Step 5: 跑測試確認通過**

Run: `cd backend_py && uv run pytest tests/test_api_schemas.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/api/deps.py backend_py/src/bfx_funding_bot/modules/api/schemas.py backend_py/tests/test_api_schemas.py
git commit -m "✨ Feat: api-keys deps (session/bitfinex) + camelCase schemas (SP2)"
```

---

## Task 10: api-keys router + wire into main.py

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/api/api_keys.py`
- Modify: `backend_py/src/bfx_funding_bot/main.py:33`
- Test: `backend_py/tests/test_api_keys_router.py`

- [ ] **Step 1: 寫失敗測試（TestClient + dependency overrides + sqlite session factory）**

`tests/test_api_keys_router.py`：

```python
import base64

import bfx_funding_bot.modules.accounts.tables  # noqa: F401
import bfx_funding_bot.modules.accounts.user_profile  # noqa: F401
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.core.auth import Principal, require_user
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.modules.api.api_keys import build_api_keys_router
from bfx_funding_bot.modules.api.deps import get_bitfinex_auth_rest, get_session

_KEK_B64 = base64.b64encode(bytes(range(32))).decode()


class _FakeClient:
    def __init__(self, perms=None, error=None):
        self._perms = perms
        self._error = error

    async def get_key_permissions(self, *, ctx):
        if self._error:
            raise self._error
        return self._perms


@pytest_asyncio.fixture
async def app_client(sqlite_engine, monkeypatch):
    monkeypatch.setenv("BFX_VAULT_KEK", _KEK_B64)
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)

    app = FastAPI()
    app.include_router(build_api_keys_router())

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

    fake = _FakeClient(perms=KeyPermissions(scopes={"funding": (True, True), "withdraw": (False, False)}))

    async def _override_client():
        yield fake

    app.dependency_overrides[require_user] = _fake_user
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_bitfinex_auth_rest] = _override_client
    client = TestClient(app)
    client._fake = fake  # let tests mutate perms/error
    return client


def test_create_then_list_masks_secret(app_client):
    r = app_client.post("/api/v1/api-keys", json={"label": "main", "apiKey": "PUB", "apiSecret": "SEC"})
    assert r.status_code == 201, r.text
    body = r.json()["data"]
    assert body["apiKey"] == "PUB"
    assert body["apiSecret"] == "****"
    assert body["exchangeStatus"] == "unverified"

    r2 = app_client.get("/api/v1/api-keys")
    assert r2.status_code == 200
    items = r2.json()["data"]
    assert len(items) == 1
    assert items[0]["apiSecret"] == "****"


def test_duplicate_returns_409(app_client):
    app_client.post("/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"})
    r = app_client.post("/api/v1/api-keys", json={"label": "b", "apiKey": "P2", "apiSecret": "S2"})
    assert r.status_code == 409


def test_verify_marks_verified(app_client):
    created = app_client.post("/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}).json()["data"]
    r = app_client.post(f"/api/v1/api-keys/{created['id']}/verify")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "verified"


def test_verify_withdraw_enabled_fails(app_client):
    created = app_client.post("/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}).json()["data"]
    app_client._fake._perms = KeyPermissions(scopes={"funding": (True, True), "withdraw": (False, True)})
    r = app_client.post(f"/api/v1/api-keys/{created['id']}/verify")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "failed"
    assert r.json()["data"]["error"] == "withdraw_must_be_disabled"


def test_verify_unknown_id_404(app_client):
    import uuid
    r = app_client.post(f"/api/v1/api-keys/{uuid.uuid4()}/verify")
    assert r.status_code == 404


def test_delete(app_client):
    created = app_client.post("/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}).json()["data"]
    r = app_client.delete(f"/api/v1/api-keys/{created['id']}")
    assert r.status_code == 204
    assert app_client.get("/api/v1/api-keys").json()["data"] == []


def test_requires_auth():
    app = FastAPI()
    app.include_router(build_api_keys_router())
    c = TestClient(app)
    assert c.get("/api/v1/api-keys").status_code in (401, 403)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd backend_py && uv run pytest tests/test_api_keys_router.py -v`
Expected: FAIL — `ModuleNotFoundError: ...api.api_keys`。

- [ ] **Step 3: 實作 router**

`modules/api/api_keys.py`：

```python
"""SP2 api-keys endpoints. Thin: maps vault service results to HTTP. Every route
is gated by require_user and scoped to principal.user_id. {"data": ...} envelope."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.auth import Principal, require_user
from bfx_funding_bot.core.crypto import VaultNotConfiguredError, load_kek
from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
from bfx_funding_bot.modules.accounts import vault
from bfx_funding_bot.modules.accounts.tables import APIKey
from bfx_funding_bot.modules.api.deps import get_bitfinex_auth_rest, get_session
from bfx_funding_bot.modules.api.schemas import (
    ApiKeyResponse,
    CreateApiKeyRequest,
    VerifyResultResponse,
)


def _to_response(row: APIKey) -> dict[str, object]:
    return ApiKeyResponse(
        id=str(row.id),
        label=row.label,
        api_key=row.api_key,
        exchange_status=row.exchange_status,
        created_at=row.created_at.isoformat() if row.created_at else "",
        verified_at=row.verified_at.isoformat() if row.verified_at else None,
    ).model_dump(by_alias=True)


def _require_kek() -> bytes:
    try:
        return load_kek()
    except VaultNotConfiguredError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="vault_not_configured"
        ) from e


def build_api_keys_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["api-keys"])

    @router.get("/api-keys")
    async def list_keys(  # noqa: ANN202
        user: Principal = Depends(require_user),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ):
        rows = await vault.list_api_keys(session, user_id=user.user_id)
        return {"data": [_to_response(r) for r in rows]}

    @router.post("/api-keys", status_code=status.HTTP_201_CREATED)
    async def create_key(  # noqa: ANN202
        body: CreateApiKeyRequest,
        user: Principal = Depends(require_user),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ):
        kek = _require_kek()
        try:
            row = await vault.create_api_key(
                session, user_id=user.user_id, label=body.label,
                api_key=body.api_key, api_secret=body.api_secret, kek=kek,
            )
        except vault.KeyAlreadyExists as e:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="key_already_exists"
            ) from e
        return {"data": _to_response(row)}

    @router.post("/api-keys/{key_id}/verify")
    async def verify_key(  # noqa: ANN202
        key_id: UUID,
        user: Principal = Depends(require_user),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
        client: BitfinexAuthREST = Depends(get_bitfinex_auth_rest),  # noqa: B008
    ):
        kek = _require_kek()
        try:
            row = await vault.verify_api_key(
                session, client, user_id=user.user_id, key_id=key_id, kek=kek,
            )
        except BitfinexAPIError as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="exchange_unreachable"
            ) from e
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
        return {"data": VerifyResultResponse(
            status=row.exchange_status, error=row.last_verify_error
        ).model_dump()}

    @router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_key(  # noqa: ANN202
        key_id: UUID,
        user: Principal = Depends(require_user),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ):
        deleted = await vault.delete_api_key(session, user_id=user.user_id, key_id=key_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
```

- [ ] **Step 4: Wire into main.py**

`main.py` 行 9 import 之後加，並在行 33 之後 include：

```python
from bfx_funding_bot.modules.api.api_keys import build_api_keys_router as build_api_keys
```

行 33（`app.include_router(build_api_router())`）之後加：

```python
app.include_router(build_api_keys())
```

- [ ] **Step 5: 跑 router 測試 + 全 unit 測試確認通過**

Run: `cd backend_py && uv run pytest tests/test_api_keys_router.py -v && uv run pytest -m "not integration" -q`
Expected: router PASS（7 passed）；全 unit suite 綠。

- [ ] **Step 6: lint + type**

Run: `cd backend_py && uv run ruff check && uv run mypy src/`
Expected: 全綠。

- [ ] **Step 7: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/api/api_keys.py backend_py/src/bfx_funding_bot/main.py backend_py/tests/test_api_keys_router.py
git commit -m "✨ Feat: api-keys CRUD + verify router (SP2)"
```

---

## Task 11: FE Server Action + hook + Vitest

**Files:**
- Create: `frontend/src/app/[locale]/(dashboard)/api-keys/actions.ts`
- Modify: `frontend/src/features/api-keys/hooks/use-api-keys.ts:13-22`
- Test: `frontend/src/features/api-keys/hooks/__tests__/create-action.test.ts`

- [ ] **Step 1: 寫失敗測試（Vitest，mock auth + fetch）**

`frontend/src/features/api-keys/hooks/__tests__/create-action.test.ts`：

```typescript
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/headers", () => ({ headers: async () => new Headers() }));
vi.mock("@/lib/auth", () => ({
  auth: { api: { getToken: vi.fn(async () => ({ token: "jwt-123" })) } },
}));

import { createApiKeyAction } from "@/app/[locale]/(dashboard)/api-keys/actions";

afterEach(() => vi.restoreAllMocks());

describe("createApiKeyAction", () => {
  it("posts plaintext server-side with bearer and returns data", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ data: { id: "1", label: "main", apiKey: "PUB", apiSecret: "****", exchangeStatus: "unverified", createdAt: "x" } }), { status: 201 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await createApiKeyAction({ label: "main", apiKey: "PUB", apiSecret: "SEC" });

    expect(result.apiKey).toBe("PUB");
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/v1/api-keys");
    expect((init as RequestInit).method).toBe("POST");
    expect((init as RequestInit).body).toContain("SEC"); // plaintext only in server fetch body
    expect((init as any).headers.Authorization).toBe("Bearer jwt-123");
  });

  it("throws on non-ok response", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify({ detail: "key_already_exists" }), { status: 409 }),
    ));
    await expect(
      createApiKeyAction({ label: "x", apiKey: "P", apiSecret: "S" }),
    ).rejects.toThrow("key_already_exists");
  });
});
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd frontend && pnpm test src/features/api-keys/hooks/__tests__/create-action.test.ts`
Expected: FAIL — 找不到 `createApiKeyAction`。

- [ ] **Step 3: 實作 Server Action**

`frontend/src/app/[locale]/(dashboard)/api-keys/actions.ts`：

```typescript
"use server";

import { headers } from "next/headers";
import { auth } from "@/lib/auth";
import type { ApiKey } from "@/types";

// eslint-disable-next-line -- server-only, validated by env.ts at startup
const API_URL = process.env.API_URL as string;

/**
 * Create a Bitfinex API key. The plaintext secret travels browser -> this
 * Server Action (server-side) -> the FastAPI web-API, NEVER through the client
 * /api/proxy hop. The web-API envelope-encrypts it before persisting.
 */
export async function createApiKeyAction(input: {
  label: string;
  apiKey: string;
  apiSecret: string;
}): Promise<ApiKey> {
  let token: string | undefined;
  try {
    const res = await auth.api.getToken({ headers: await headers() });
    token = res?.token;
  } catch {
    token = undefined;
  }

  const res = await fetch(new URL("/api/v1/api-keys", API_URL).toString(), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(input),
  });

  const text = await res.text();
  if (!res.ok) {
    let message = "Failed to save API key";
    try {
      const parsed = JSON.parse(text) as { detail?: string; error?: { message?: string } };
      message = parsed.detail ?? parsed.error?.message ?? message;
    } catch {
      /* keep default */
    }
    throw new Error(message);
  }
  return (JSON.parse(text) as { data: ApiKey }).data;
}
```

- [ ] **Step 4: 改 hook 用 action**

`frontend/src/features/api-keys/hooks/use-api-keys.ts`：頂部加 import，`useCreateApiKey` 的 `mutationFn` 改呼 action：

```typescript
import { createApiKeyAction } from "@/app/[locale]/(dashboard)/api-keys/actions";
```

把 `useCreateApiKey`（行 13-22）的 `mutationFn` 改為：

```typescript
    mutationFn: (data: { label: string; apiKey: string; apiSecret: string }) =>
      createApiKeyAction(data),
```

（其餘 `useApiKeys` / `useDeleteApiKey` / `useVerifyApiKey` 不變 — 無明文，仍走 client proxy。）

- [ ] **Step 5: 跑測試 + lint 確認通過**

Run: `cd frontend && pnpm test src/features/api-keys/hooks/__tests__/create-action.test.ts && pnpm lint`
Expected: PASS（2 passed）+ lint 綠。

- [ ] **Step 6: Commit**

```bash
git add frontend/src/app/[locale]/\(dashboard\)/api-keys/actions.ts frontend/src/features/api-keys/hooks/use-api-keys.ts frontend/src/features/api-keys/hooks/__tests__/create-action.test.ts
git commit -m "✨ Feat: api-key create via Server Action (no client proxy hop) (SP2)"
```

---

## Task 12: 部署增量 — `deploy-vm.sh` BFX_VAULT_KEK + runbook

**Files:**
- Modify: `scripts/deploy-vm.sh`（webapi.env 組裝段；參考 SP1 `DATABASE_URL` / `BETTER_AUTH_JWKS_URL` 註記）

- [ ] **Step 1: 看現況**

Run: `grep -n "webapi\|WEBAPI\|JWKS\|DATABASE_URL\|\.env.webapi.runtime" scripts/deploy-vm.sh`
Expected: 找到 SP1 組裝 `.env.webapi.runtime` 的區段（約行 29-36）。

- [ ] **Step 2: 加 BFX_VAULT_KEK 到 webapi preflight（form a 已確認）**

> **[Plan amendment 2026-06-07 — review]** 已驗證 SP1 的組裝是 **form (a) 整檔 `cp`**：`scripts/deploy-vm.sh:30-33` 設 `WEBAPI_SECRETS="$HOME/bfx/webapi.env"` → `cp "$WEBAPI_SECRETS" .env.webapi.runtime` → `chmod 600`。webapi.env **從不被 `source`** 進 shell scope（只 `cp` + grep 驗證）。所以原計畫的 `echo "BFX_VAULT_KEK=${BFX_VAULT_KEK:?...}" >> .env.webapi.runtime` 會把 `$BFX_VAULT_KEK` 展開成**空字串**（變數不在 scope）——**不可使用**。

正確做法（**不改 cp 邏輯**，只加 fail-fast preflight）：把 `BFX_VAULT_KEK` 加進既有 webapi preflight for-loop（`scripts/deploy-vm.sh:34`，比照 `DATABASE_URL` / `BETTER_AUTH_JWKS_URL`，缺/空即 `exit 1`）：

```bash
# 由：
for v in DATABASE_URL BETTER_AUTH_JWKS_URL; do
# 改為：
for v in DATABASE_URL BETTER_AUTH_JWKS_URL BFX_VAULT_KEK; do
```

（line 35 的錯誤訊息模板已內插 `$v` 與 `$WEBAPI_SECRETS`，會自動對 `BFX_VAULT_KEK` 產生正確訊息，無需其他改動。`BFX_VAULT_KEK` 只屬 webapi loop，**勿**加進 daemon-side 的 `need_common`/`need_canary` 區塊。）整檔 `cp` 會把 `~/bfx/webapi.env` 內的 `BFX_VAULT_KEK=` 自動帶進 `.env.webapi.runtime`，runbook（Step 5）負責確保它在該檔內。

- [ ] **Step 3: 驗證腳本語法**

Run: `bash -n scripts/deploy-vm.sh`
Expected: 無語法錯誤輸出。

- [ ] **Step 4: Commit**

```bash
git add scripts/deploy-vm.sh
git commit -m "🚀 Deploy: pass BFX_VAULT_KEK into webapi.env assembly (SP2)"
```

- [ ] **Step 5: 寫部署 runbook（手動步驟，Will 有 prod 存取時執行；不需測試）**

把以下追加到 spec 或一個 `docs/superpowers/` runbook 註記（executor 只需確認檔案存在、內容正確，不執行）：

```
SP2 部署 runbook（Will 手動）：
1. 產 KEK：openssl rand -base64 32  → 寫入 ~/bfx/webapi.env 的 BFX_VAULT_KEK=，並複製進 ~/second-brain/secrets/
2. psql（superuser）對 live Neon：
   GRANT SELECT, INSERT, UPDATE, DELETE ON public.api_keys TO bfx_webapi;
   （注意：SP2 migration 把 user_profiles 的 unique index 換成 constraint — 不影響既有 grants）
3. 本機改 source → VM 跑 scripts/deploy-vm.sh canary（git pull + build + migrate 套 f1a2b3c4d5e6 + recreate webapi）。絕不手改 VM。
4. Vercel：無新 env（Server Action 用既有 API_URL）。
5. smoke：FE 新增 key → verify → 綠勾；psql 確認 api_keys.secret_ciphertext 為密文、exchange_status=verified。
   ⚠️ deploy-vm.sh git pull 整個 origin/main → 先 review HEAD..origin/main 確認無其他 inert 改動上線。
```

---

## Self-Review

**Spec coverage（逐節對照 spec）：**
- §1 D1 envelope → Task 2 ✓；D2 fail-closed verify → Task 5+8 ✓；D3 單一 key → Task 3 unique index ✓；D4 只加密 secret → Task 2/3（api_key 明文欄）✓；D5 crypto 在 FastAPI → Task 2 ✓；D6 改造 dormant model → Task 3 ✓。
- §2 資料流（Server Action 不走 client proxy）→ Task 11 ✓；唯一解密=verify → Task 8 ✓。
- §3 crypto 模組 → Task 2 ✓（KEK env、per-record DEK、AAD=user_id、key_version、503 fail-loud via `_require_kek`）。
- §4 schema → Task 3+4 ✓（FK DB-level only、unique、GRANT psql）。
- §5 verify → Task 5（permissions client）+ Task 8（判定 + 502/invalid_credentials 分流）✓。
- §6 endpoints → Task 10 ✓（4 路由、404 scope、JIT via create、camelCase）；JIT helper → Task 6 ✓。
- §7 FE → Task 11 ✓。
- §8 測試 → 每 task TDD ✓（crypto/verify/router/migration 皆覆蓋）。
- §9 部署 → Task 12 ✓。
- §10 風險 → 涵蓋（fail-loud KEK、permission 解析測試、DTO alias、GRANT runbook、不 log body）。

**Placeholder scan：** 無 TBD/TODO；每 code step 有完整程式碼。Task 12 Step 2 留「依實際腳本形式擇一」是因 deploy-vm.sh 既有形式未在 plan 內展開——executor Step 1 先 grep 確認形式再擇一，非 placeholder。

**Type consistency：**
- `Envelope`（crypto.py）欄位順序 `secret_ciphertext, secret_nonce, wrapped_dek, dek_nonce, key_version` 在 Task 2/7/8 一致。
- `KeyPermissions.can(scope, *, write)` 在 Task 5/8/10 簽名一致。
- `encrypt_secret(plaintext, *, user_id, kek)` / `decrypt_secret(env, *, user_id, kek)` 跨 task 一致。
- vault 函式簽名（`create_api_key`/`list_api_keys`/`delete_api_key`/`verify_api_key`）keyword-only `user_id`/`key_id`/`kek` 在 service 與 router 呼叫端一致。
- schema response key（`apiKey`/`apiSecret`/`exchangeStatus`/`createdAt`/`verifiedAt`）與 FE `types/index.ts` 一致。
