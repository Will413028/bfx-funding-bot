# v2 Phase 2 — 資料層回填 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 fUSD/fUST 全歷史 1h × p2/p30/a30 candle + funding_stats 全部回填到 Neon，並在 backfill 過程驗證 FRR 單位推測（5/9 推測：FRR 是秒利率）。

**Architecture:** 對齊既有 candles 模組分層為 funding_stats 鋪滿 schemas/repository/service；service 層加 walking-back + resume-from-DB primitive；新增 `scripts/backfill_phase2.py` orchestrator 跑 8 series sequential + 4 項 PASS check。Phase 3 的 `market_rate_source="frr"` 切換已在 Phase 2 備好讀取 API（`get_frr_at_or_before_mts`）。

**Tech Stack:** Python 3.13 / SQLAlchemy 2.0 async / asyncpg / httpx / aiolimiter / Pydantic v2 / pytest-asyncio / pytest-httpx / Alembic（**注意：不是 Atlas**；CLAUDE.md 那段 Atlas 是舊 Go backend，本 phase 用 Alembic）。

**Spec:** `docs/superpowers/specs/2026-05-10-phase2-data-backfill-design.md`（commit `4f2f58c`）。

**執行環境前置確認（每個新 session 跑前先檢查一次）:**
```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
test -L backend_py/.env || ln -sf ../.env backend_py/.env   # repo root .env 的 symlink
cd backend_py
uv run python -c "from bfx_funding_bot.core.settings import Settings; print(Settings().database_url[:30])"
# 應印出 'postgresql://...' 或類似 — 若失敗代表 .env 沒接到
```

所有 `pytest` / `mypy` / `ruff` / `alembic` 指令都必須在 `backend_py/` 內跑（從 worktree root 跑會撞 pyenv 3.12 sqlalchemy）。

---

## File Structure

### 新模組

```
backend_py/src/bfx_funding_bot/modules/funding_stats/
├── __init__.py
├── tables.py          # FundingStatRow（從 candles/tables.py 搬來）
├── schemas.py         # Pydantic FundingStat + from_bitfinex
├── repository.py      # upsert_funding_stats / get_min_mts /
│                      # get_in_range / get_frr_at_or_before_mts
└── service.py         # backfill_to_earliest

backend_py/src/bfx_funding_bot/modules/backfill/
├── __init__.py
├── schemas.py         # SeriesSpec / BackfillStats / BackfillError
├── errors.py          # BackfillCursorStuck
└── checks.py          # 4 PASS check 函式 + summary builder
```

### 修改檔案

```
backend_py/src/bfx_funding_bot/modules/candles/tables.py        # 移除 FundingStatRow
backend_py/src/bfx_funding_bot/modules/candles/repository.py    # +get_min_mts
backend_py/src/bfx_funding_bot/modules/candles/service.py       # +backfill_candles_to_earliest
backend_py/src/bfx_funding_bot/external/bitfinex/rest.py        # +get_funding_stats
backend_py/alembic/env.py                                       # +import funding_stats.tables
```

### 新增測試

```
backend_py/tests/modules/funding_stats/
├── __init__.py
├── test_tables.py       # 從 candles/test_tables.py 搬一半
├── test_schemas.py
├── test_repository.py
└── test_walking_back.py

backend_py/tests/modules/backfill/
├── __init__.py
└── test_checks.py

backend_py/tests/modules/candles/test_walking_back.py
```

### 修改測試

```
backend_py/tests/modules/candles/test_tables.py             # 只留 FundingCandleRow
backend_py/tests/external/bitfinex/test_rest.py             # +funding_stats response tests
```

### 新增 scripts

```
backend_py/scripts/backfill_phase2.py
```

### 新增 docs

```
docs/superpowers/specs/2026-05-10-phase2-result.md
```

---

## Commit 1 — ♻️ Refactor: move FundingStatRow to funding_stats/tables.py

**Goal:** 把 `FundingStatRow` 從 `candles/tables.py` 搬到新模組 `funding_stats/tables.py`，**完全不動 schema**（`__tablename__` 仍是 `funding_stats`，欄位順序與型別都不變）。完工驗證 `alembic check` 看不到 schema drift。

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/funding_stats/__init__.py`
- Create: `backend_py/src/bfx_funding_bot/modules/funding_stats/tables.py`
- Create: `backend_py/tests/modules/funding_stats/__init__.py`
- Create: `backend_py/tests/modules/funding_stats/test_tables.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/candles/tables.py`
- Modify: `backend_py/tests/modules/candles/test_tables.py`
- Modify: `backend_py/alembic/env.py`

### Tasks

- [ ] **Step 1.1: Create new `funding_stats/__init__.py`**

```python
# backend_py/src/bfx_funding_bot/modules/funding_stats/__init__.py
```
（空檔即可。）

- [ ] **Step 1.2: Create new `funding_stats/tables.py` containing FundingStatRow**

```python
# backend_py/src/bfx_funding_bot/modules/funding_stats/tables.py
from sqlalchemy import BigInteger, Float, Index, PrimaryKeyConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base


class FundingStatRow(Base):
    __tablename__ = "funding_stats"

    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    mts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    frr: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_period: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_amount_used: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_below_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("symbol", "mts"),
        Index("idx_funding_stats_mts", "mts"),
    )
```
逐欄與既有 `candles/tables.py` 第 26-40 行對照，**順序、型別、索引名都不可改**。

- [ ] **Step 1.3: Remove FundingStatRow from `candles/tables.py`**

刪除 `backend_py/src/bfx_funding_bot/modules/candles/tables.py` 第 26-40 行（含 `class FundingStatRow` 整段），檔案剩下：

```python
from sqlalchemy import BigInteger, Float, Index, PrimaryKeyConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base


class FundingCandleRow(Base):
    __tablename__ = "funding_candles"

    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    timeframe: Mapped[str] = mapped_column(Text, nullable=False)
    period_agg: Mapped[str] = mapped_column(Text, nullable=False)
    mts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    open: Mapped[float | None] = mapped_column(Float, nullable=True)
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("symbol", "timeframe", "period_agg", "mts"),
        Index("idx_funding_candles_mts", "mts"),
    )
```

- [ ] **Step 1.4: Add side-effect import to `alembic/env.py`**

在 `backend_py/alembic/env.py` 第 17 行（`import bfx_funding_bot.modules.candles.tables`）下方加：

```python
import bfx_funding_bot.modules.funding_stats.tables  # noqa: F401
```

確認 `noqa: F401` 註解保留 — 這 import 是 side-effect-only，註冊 table 到 `Base.metadata`。

- [ ] **Step 1.5: Create test directory + test file for funding_stats tables**

```python
# backend_py/tests/modules/funding_stats/__init__.py
```
（空檔即可。）

```python
# backend_py/tests/modules/funding_stats/test_tables.py
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.funding_stats.tables import FundingStatRow


@pytest.mark.asyncio
async def test_funding_stat_table_creates_on_sqlite(sqlite_engine: AsyncEngine) -> None:
    """Smoke: FundingStatRow ORM table emits DDL without errors."""
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    table_names = set(Base.metadata.tables.keys())
    assert "funding_stats" in table_names

    pk_cols = [c.name for c in FundingStatRow.__table__.primary_key]
    assert pk_cols == ["symbol", "mts"]
```

- [ ] **Step 1.6: Trim `tests/modules/candles/test_tables.py` to only candle assertions**

替換整個檔案內容為：

```python
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.candles.tables import FundingCandleRow


@pytest.mark.asyncio
async def test_candle_table_creates_on_sqlite(sqlite_engine: AsyncEngine) -> None:
    """Smoke: FundingCandleRow ORM table emits DDL without errors."""
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    table_names = set(Base.metadata.tables.keys())
    assert "funding_candles" in table_names

    pk_cols = [c.name for c in FundingCandleRow.__table__.primary_key]
    assert pk_cols == ["symbol", "timeframe", "period_agg", "mts"]
```

- [ ] **Step 1.7: Run all tests — expect both old and new test files pass**

Run: `cd backend_py && uv run pytest -m "not integration" -v`
Expected: 全綠（既有 candle / accounts / backtest / health 測試 + 新增 funding_stats test_tables）

- [ ] **Step 1.8: Run mypy + ruff**

Run: `cd backend_py && uv run mypy src/ && uv run ruff check`
Expected: 兩者皆無錯。若 ruff 嫌 `noqa: F401` 多餘，看哪個版本要怎麼寫；若沒抱怨就維持。

- [ ] **Step 1.9: Verify schema drift = none via `alembic check`**

Run:
```bash
cd backend_py && uv run alembic check
```
Expected: `No new upgrade operations detected.` — 模組 Python 路徑改了但 table 定義 byte-identical，metadata 與 Neon 已有 schema 完全相同。

若失敗（顯示有 drift）→ 表示 Step 1.2 的 column 定義不慎與原本不同，回去逐字 diff 修正。

- [ ] **Step 1.10: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/funding_stats/ \
        backend_py/src/bfx_funding_bot/modules/candles/tables.py \
        backend_py/tests/modules/funding_stats/ \
        backend_py/tests/modules/candles/test_tables.py \
        backend_py/alembic/env.py
git commit -m "♻️ Refactor: move FundingStatRow to funding_stats/tables.py

Pure module-path move; table schema byte-identical (verified by alembic check).
Prepares funding_stats module for Phase 2 schemas/repo/service buildup."
```

---

## Commit 2 — ✨ Feat: funding_stats schemas + repository

**Goal:** 完成 `funding_stats/{schemas,repository}.py`。第一步 curl 真 Bitfinex 確認 array shape，再寫 from_bitfinex；repository 提供 upsert + 三個讀取 API（含 Phase 3 要用的 `get_frr_at_or_before_mts`）。

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/funding_stats/schemas.py`
- Create: `backend_py/src/bfx_funding_bot/modules/funding_stats/repository.py`
- Create: `backend_py/tests/modules/funding_stats/test_schemas.py`
- Create: `backend_py/tests/modules/funding_stats/test_repository.py`

### Tasks

- [ ] **Step 2.1: Curl real Bitfinex `/v2/funding/stats` — verify array shape**

Run:
```bash
curl -s 'https://api-pub.bitfinex.com/v2/funding/stats/fUSD/hist?limit=1' | python3 -m json.tool
```
Expected output 形式（範例）：
```json
[
    [
        1715000000000,    // [0] MTS
        5.8e-7,            // [1] FRR
        2.3,               // [2] AVG_PERIOD
        null, null, null, null, null, null, null,   // [3-9] placeholders
        4.5e7,             // [10] FUNDING_AMOUNT
        2.1e7,             // [11] FUNDING_AMOUNT_USED
        null, null, null,  // [12-14] placeholders
        1.2e6              // [15] FUNDING_BELOW_THRESHOLD
    ]
]
```

**驗證 checklist：**
1. 回傳是 array of arrays
2. 內層 array 長度 ≥ 16
3. raw[0] 是 13 位數 ms timestamp
4. raw[1] 是極小的 float（< 1e-3，符合「秒利率」推測）
5. raw[10]、raw[11] 是大數字（資金量，USD）

若 shape 與上方註解不一致 → **本 commit 後續所有 `from_bitfinex` 索引必須對齊實測 shape**。把實測一筆 raw 例子貼到 `funding_stats/schemas.py` 檔頭 docstring。

- [ ] **Step 2.2: Write failing test for `FundingStat.from_bitfinex`**

Create: `backend_py/tests/modules/funding_stats/test_schemas.py`

```python
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from bfx_funding_bot.modules.funding_stats.schemas import FundingStat


def test_funding_stat_from_bitfinex_array() -> None:
    """Bitfinex /v2/funding/stats array shape (16 entries):
    [MTS, FRR, AVG_PERIOD, _, _, _, _, _, _, _,
     FUNDING_AMOUNT, FUNDING_AMOUNT_USED, _, _, _,
     FUNDING_BELOW_THRESHOLD]
    """
    raw = [
        1715000000000,
        5.8e-7,
        2.3,
        None, None, None, None, None, None, None,
        4.5e7,
        2.1e7,
        None, None, None,
        1.2e6,
    ]
    fs = FundingStat.from_bitfinex(raw, symbol="fUSD")

    assert fs.symbol == "fUSD"
    assert fs.mts == 1715000000000
    assert fs.frr == Decimal("5.8e-7")
    assert fs.avg_period == Decimal("2.3")
    assert fs.funding_amount == Decimal("45000000.0")
    assert fs.funding_amount_used == Decimal("21000000.0")
    assert fs.funding_below_threshold == Decimal("1200000.0")


def test_funding_stat_decimal_precision() -> None:
    raw = [1715000000000, 5.823456789e-7, 2.3, None, None, None, None,
           None, None, None, 4.5e7, 2.1e7, None, None, None, 1.2e6]
    fs = FundingStat.from_bitfinex(raw, symbol="fUSD")
    assert fs.frr is not None
    assert str(fs.frr).startswith("5.823456789")  # no float drift


def test_funding_stat_handles_null_fields() -> None:
    raw = [1715000000000, None, None, None, None, None, None,
           None, None, None, None, None, None, None, None, None]
    fs = FundingStat.from_bitfinex(raw, symbol="fUSD")
    assert fs.frr is None
    assert fs.funding_amount is None


def test_funding_stat_timestamp_helper() -> None:
    raw = [1704067200000, 0.0, 0.0, None, None, None, None,
           None, None, None, 0.0, 0.0, None, None, None, 0.0]
    fs = FundingStat.from_bitfinex(raw, symbol="fUSD")
    assert fs.timestamp() == datetime(2024, 1, 1, 0, 0, tzinfo=UTC)


def test_funding_stat_rejects_short_array() -> None:
    raw = [1715000000000, 5.8e-7, 2.3]  # too short
    with pytest.raises((ValidationError, ValueError)):
        FundingStat.from_bitfinex(raw, symbol="fUSD")
```

- [ ] **Step 2.3: Run test — verify it fails with import error**

Run: `cd backend_py && uv run pytest tests/modules/funding_stats/test_schemas.py -v`
Expected: 5 tests FAIL — `ModuleNotFoundError: No module named 'bfx_funding_bot.modules.funding_stats.schemas'`

- [ ] **Step 2.4: Implement `FundingStat`**

Create: `backend_py/src/bfx_funding_bot/modules/funding_stats/schemas.py`

```python
"""Pydantic schema for Bitfinex /v2/funding/stats rows.

Real-Bitfinex array shape (verified by curl in Commit 2 first step, 2026-05-10):
[MTS, FRR, AVG_PERIOD, _, _, _, _, _, _, _,
 FUNDING_AMOUNT, FUNDING_AMOUNT_USED, _, _, _,
 FUNDING_BELOW_THRESHOLD]

Sample raw row (paste actual curl output here when running Commit 2 Step 2.1):
[1715000000000, 5.8e-7, 2.3, null, null, null, null, null, null, null,
 4.5e7, 2.1e7, null, null, null, 1.2e6]
"""
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _to_decimal(value: float | int | str | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


class FundingStat(BaseModel):
    """Funding-stats row from Bitfinex /v2/funding/stats endpoint.

    Mirrors the funding_stats table PK (symbol, mts).
    Numeric fields use Decimal to avoid float drift.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    symbol: str = Field(min_length=1)
    mts: int  # millisecond timestamp
    frr: Decimal | None = None
    avg_period: Decimal | None = None
    funding_amount: Decimal | None = None
    funding_amount_used: Decimal | None = None
    funding_below_threshold: Decimal | None = None

    @field_validator(
        "frr", "avg_period", "funding_amount",
        "funding_amount_used", "funding_below_threshold",
        mode="before",
    )
    @classmethod
    def _coerce_decimal(cls, v: Any) -> Decimal | None:
        return _to_decimal(v)

    def timestamp(self) -> datetime:
        return datetime.fromtimestamp(self.mts / 1000, tz=UTC)

    @classmethod
    def from_bitfinex(cls, raw: list[Any], *, symbol: str) -> "FundingStat":
        """Parse Bitfinex array shape — see module docstring for layout.

        Required min length 16 (raw[15] = FUNDING_BELOW_THRESHOLD).
        """
        if len(raw) < 16:
            raise ValueError(
                f"Expected ≥16 elements in Bitfinex funding_stats row, "
                f"got {len(raw)}: {raw!r}"
            )
        return cls(
            symbol=symbol,
            mts=int(raw[0]),
            frr=_to_decimal(raw[1]),
            avg_period=_to_decimal(raw[2]),
            funding_amount=_to_decimal(raw[10]),
            funding_amount_used=_to_decimal(raw[11]),
            funding_below_threshold=_to_decimal(raw[15]),
        )
```

- [ ] **Step 2.5: Run schemas test — expect pass**

Run: `cd backend_py && uv run pytest tests/modules/funding_stats/test_schemas.py -v`
Expected: 5 PASSED

- [ ] **Step 2.6: Write failing test for repository — upsert + read-back**

Create: `backend_py/tests/modules/funding_stats/test_repository.py`

```python
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.funding_stats.repository import (
    get_frr_at_or_before_mts,
    get_in_range,
    get_min_mts,
    upsert_funding_stats,
)
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat


@pytest.fixture
async def setup_schema(sqlite_engine: AsyncEngine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _make(symbol: str, mts: int, frr: str = "5.8e-7") -> FundingStat:
    return FundingStat(
        symbol=symbol,
        mts=mts,
        frr=Decimal(frr),
        avg_period=Decimal("2.3"),
        funding_amount=Decimal("45000000"),
        funding_amount_used=Decimal("21000000"),
        funding_below_threshold=Decimal("1200000"),
    )


@pytest.mark.asyncio
async def test_upsert_then_get_in_range_round_trips(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    rows = [
        _make("fUSD", 1700000000000, frr="5.8e-7"),
        _make("fUSD", 1700003600000, frr="6.1e-7"),
    ]
    await upsert_funding_stats(sqlite_session, rows)
    await sqlite_session.commit()

    fetched = await get_in_range(
        sqlite_session, symbol="fUSD",
        start_mts=1700000000000, end_mts=1700003600000,
    )
    assert len(fetched) == 2
    assert fetched[0].mts == 1700000000000
    assert fetched[1].mts == 1700003600000
    assert fetched[0].frr is not None
    assert abs(fetched[0].frr - Decimal("5.8e-7")) < Decimal("1e-12")


@pytest.mark.asyncio
async def test_upsert_replaces_existing_pk(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    original = _make("fUSD", 1700000000000, frr="5.8e-7")
    updated = original.model_copy(update={"frr": Decimal("7.0e-7")})

    await upsert_funding_stats(sqlite_session, [original])
    await upsert_funding_stats(sqlite_session, [updated])
    await sqlite_session.commit()

    fetched = await get_in_range(
        sqlite_session, symbol="fUSD",
        start_mts=1700000000000, end_mts=1700000000000,
    )
    assert len(fetched) == 1
    assert fetched[0].frr is not None
    assert abs(fetched[0].frr - Decimal("7.0e-7")) < Decimal("1e-12")


@pytest.mark.asyncio
async def test_upsert_empty_list_is_noop(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    await upsert_funding_stats(sqlite_session, [])
    await sqlite_session.commit()
    fetched = await get_in_range(
        sqlite_session, symbol="fUSD", start_mts=0, end_mts=10**13,
    )
    assert fetched == []


@pytest.mark.asyncio
async def test_get_min_mts_returns_none_for_empty_symbol(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    result = await get_min_mts(sqlite_session, symbol="fUSD")
    assert result is None


@pytest.mark.asyncio
async def test_get_min_mts_returns_smallest(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    await upsert_funding_stats(
        sqlite_session,
        [
            _make("fUSD", 1700003600000),
            _make("fUSD", 1700000000000),
            _make("fUSD", 1700007200000),
            _make("fUST", 1500000000000),  # different symbol
        ],
    )
    await sqlite_session.commit()
    assert await get_min_mts(sqlite_session, symbol="fUSD") == 1700000000000
    assert await get_min_mts(sqlite_session, symbol="fUST") == 1500000000000


@pytest.mark.asyncio
async def test_get_frr_at_or_before_mts_returns_latest_le(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    await upsert_funding_stats(
        sqlite_session,
        [
            _make("fUSD", 1700000000000, frr="5.0e-7"),
            _make("fUSD", 1700003600000, frr="6.0e-7"),
            _make("fUSD", 1700007200000, frr="7.0e-7"),
        ],
    )
    await sqlite_session.commit()

    # exact match
    fs = await get_frr_at_or_before_mts(
        sqlite_session, symbol="fUSD", mts=1700003600000,
    )
    assert fs is not None
    assert fs.mts == 1700003600000
    assert fs.frr is not None
    assert abs(fs.frr - Decimal("6.0e-7")) < Decimal("1e-12")

    # between two entries — return earlier
    fs = await get_frr_at_or_before_mts(
        sqlite_session, symbol="fUSD", mts=1700005000000,
    )
    assert fs is not None
    assert fs.mts == 1700003600000

    # before all entries
    fs = await get_frr_at_or_before_mts(
        sqlite_session, symbol="fUSD", mts=1500000000000,
    )
    assert fs is None


@pytest.mark.asyncio
async def test_get_frr_at_or_before_mts_isolated_per_symbol(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    await upsert_funding_stats(
        sqlite_session,
        [
            _make("fUSD", 1700000000000, frr="5.0e-7"),
            _make("fUST", 1700003600000, frr="9.9e-7"),
        ],
    )
    await sqlite_session.commit()

    fs = await get_frr_at_or_before_mts(
        sqlite_session, symbol="fUSD", mts=1700010000000,
    )
    assert fs is not None
    assert fs.mts == 1700000000000  # 不會抓到 fUST 那筆
```

- [ ] **Step 2.7: Run test — expect import errors**

Run: `cd backend_py && uv run pytest tests/modules/funding_stats/test_repository.py -v`
Expected: 7 tests FAIL with `ModuleNotFoundError`.

- [ ] **Step 2.8: Implement repository**

Create: `backend_py/src/bfx_funding_bot/modules/funding_stats/repository.py`

```python
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.postgresql.dml import Insert as PGInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.sqlite.dml import Insert as SQLiteInsert
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from bfx_funding_bot.modules.funding_stats.tables import FundingStatRow

_UPSERT_INDEX = ["symbol", "mts"]
_UPSERT_SET_COLS = [
    "frr",
    "avg_period",
    "funding_amount",
    "funding_amount_used",
    "funding_below_threshold",
]


def _to_float_or_none(d: Decimal | None) -> float | None:
    return float(d) if d is not None else None


def _row_to_domain(row: FundingStatRow) -> FundingStat:
    return FundingStat(
        symbol=row.symbol,
        mts=row.mts,
        frr=Decimal(str(row.frr)) if row.frr is not None else None,
        avg_period=Decimal(str(row.avg_period)) if row.avg_period is not None else None,
        funding_amount=(
            Decimal(str(row.funding_amount)) if row.funding_amount is not None else None
        ),
        funding_amount_used=(
            Decimal(str(row.funding_amount_used))
            if row.funding_amount_used is not None
            else None
        ),
        funding_below_threshold=(
            Decimal(str(row.funding_below_threshold))
            if row.funding_below_threshold is not None
            else None
        ),
    )


async def upsert_funding_stats(
    session: AsyncSession,
    stats: list[FundingStat],
) -> None:
    """Upsert funding_stats rows by composite PK (symbol, mts)."""
    if not stats:
        return

    rows = [
        {
            "symbol": s.symbol,
            "mts": s.mts,
            "frr": _to_float_or_none(s.frr),
            "avg_period": _to_float_or_none(s.avg_period),
            "funding_amount": _to_float_or_none(s.funding_amount),
            "funding_amount_used": _to_float_or_none(s.funding_amount_used),
            "funding_below_threshold": _to_float_or_none(s.funding_below_threshold),
        }
        for s in stats
    ]

    dialect_name = session.bind.dialect.name if session.bind else "postgresql"
    stmt: PGInsert | SQLiteInsert
    if dialect_name == "postgresql":
        stmt = pg_insert(FundingStatRow).values(rows)
    else:
        stmt = sqlite_insert(FundingStatRow).values(rows)

    stmt = stmt.on_conflict_do_update(
        index_elements=_UPSERT_INDEX,
        set_={col: getattr(stmt.excluded, col) for col in _UPSERT_SET_COLS},
    )
    await session.execute(stmt)


async def get_in_range(
    session: AsyncSession,
    *,
    symbol: str,
    start_mts: int,
    end_mts: int,
) -> list[FundingStat]:
    """Fetch funding_stats in [start_mts, end_mts] (inclusive) ASC by mts."""
    stmt = (
        select(FundingStatRow)
        .where(
            FundingStatRow.symbol == symbol,
            FundingStatRow.mts >= start_mts,
            FundingStatRow.mts <= end_mts,
        )
        .order_by(FundingStatRow.mts.asc())
    )
    result = await session.execute(stmt)
    return [_row_to_domain(row) for row in result.scalars().all()]


async def get_min_mts(session: AsyncSession, *, symbol: str) -> int | None:
    """Return smallest mts for symbol, or None if no rows."""
    from sqlalchemy import func

    stmt = select(func.min(FundingStatRow.mts)).where(FundingStatRow.symbol == symbol)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_frr_at_or_before_mts(
    session: AsyncSession,
    *,
    symbol: str,
    mts: int,
) -> FundingStat | None:
    """Return latest FundingStat with mts <= given mts (for FRR-as-market_rate
    in Phase 3). Returns None if nothing exists at or before that point."""
    stmt = (
        select(FundingStatRow)
        .where(FundingStatRow.symbol == symbol, FundingStatRow.mts <= mts)
        .order_by(FundingStatRow.mts.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    row = result.scalars().first()
    return _row_to_domain(row) if row is not None else None
```

- [ ] **Step 2.9: Run repository tests — expect pass**

Run: `cd backend_py && uv run pytest tests/modules/funding_stats/test_repository.py -v`
Expected: 7 PASSED

- [ ] **Step 2.10: Run full test suite + mypy + ruff**

Run:
```bash
cd backend_py && uv run pytest -m "not integration" -v && \
  uv run mypy src/ && uv run ruff check
```
Expected: 全綠 + 無 lint/type 錯。

- [ ] **Step 2.11: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/funding_stats/schemas.py \
        backend_py/src/bfx_funding_bot/modules/funding_stats/repository.py \
        backend_py/tests/modules/funding_stats/test_schemas.py \
        backend_py/tests/modules/funding_stats/test_repository.py
git commit -m "✨ Feat: funding_stats schemas + repository

- FundingStat Pydantic schema with from_bitfinex parser (16-element array)
- upsert_funding_stats / get_in_range / get_min_mts / get_frr_at_or_before_mts
- Bitfinex array shape verified by curl /v2/funding/stats/fUSD/hist?limit=1
- get_frr_at_or_before_mts ready for Phase 3 market_rate_source=\"frr\" wire-up"
```

---

## Commit 3 — ✨ Feat: BitfinexREST.get_funding_stats

**Goal:** 在 `BitfinexREST` 加 `get_funding_stats(symbol, end, limit)` 方法，對稱於 `get_funding_candles`：同樣走 `FundingRateLimiter`、同樣 raise `BitfinexAPIError` / `BitfinexRateLimited` / `BitfinexShapeError`、回 `list[FundingStat]`。

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/external/bitfinex/rest.py`
- Modify: `backend_py/tests/external/bitfinex/test_rest.py`

### Tasks

- [ ] **Step 3.1: Write failing tests for get_funding_stats**

把以下測試**追加**到 `backend_py/tests/external/bitfinex/test_rest.py` 末尾（保留所有既有測試）：

```python
@pytest.mark.asyncio
async def test_get_funding_stats_happy_path(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api-pub.bitfinex.com/v2/funding/stats/fUSD/hist?limit=2&end=1715000000000",
        json=[
            [1715000000000, 5.8e-7, 2.3, None, None, None, None,
             None, None, None, 4.5e7, 2.1e7, None, None, None, 1.2e6],
            [1714996400000, 5.7e-7, 2.4, None, None, None, None,
             None, None, None, 4.4e7, 2.0e7, None, None, None, 1.1e6],
        ],
    )

    async with httpx.AsyncClient() as http:
        client = BitfinexREST(
            http=http,
            base_url="https://api-pub.bitfinex.com",
            limiter=FundingRateLimiter(max_rate=1000, time_period=1.0),
        )
        stats = await client.get_funding_stats(
            symbol="fUSD", end=1715000000000, limit=2,
        )

    assert len(stats) == 2
    assert stats[0].mts == 1715000000000
    assert stats[0].frr == Decimal("5.8e-7")
    assert stats[0].funding_amount == Decimal("45000000.0")
    assert all(s.symbol == "fUSD" for s in stats)


@pytest.mark.asyncio
async def test_get_funding_stats_strips_leading_f(httpx_mock: HTTPXMock) -> None:
    """Symbol 'fUSD' becomes 'fUSD' in URL path (the leading 'f' stays).

    Bitfinex's funding_stats path is /v2/funding/stats/{Symbol}/hist where
    Symbol is the full funding symbol (with leading f). Verify call shape.
    """
    httpx_mock.add_response(
        url="https://api-pub.bitfinex.com/v2/funding/stats/fUSD/hist?limit=1&end=0",
        json=[[
            1715000000000, 5.8e-7, 2.3, None, None, None, None,
            None, None, None, 4.5e7, 2.1e7, None, None, None, 1.2e6,
        ]],
    )

    async with httpx.AsyncClient() as http:
        client = BitfinexREST(
            http=http,
            base_url="https://api-pub.bitfinex.com",
            limiter=FundingRateLimiter(max_rate=1000, time_period=1.0),
        )
        stats = await client.get_funding_stats(symbol="fUSD", end=0, limit=1)
    assert len(stats) == 1


@pytest.mark.asyncio
async def test_get_funding_stats_429_raises_rate_limited(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="https://api-pub.bitfinex.com/v2/funding/stats/fUSD/hist?limit=10&end=0",
        status_code=429,
        headers={"Retry-After": "8"},
        text='["error", 11010, "ratelimit: error"]',
    )

    async with httpx.AsyncClient() as http:
        client = BitfinexREST(
            http=http,
            base_url="https://api-pub.bitfinex.com",
            limiter=FundingRateLimiter(max_rate=1000, time_period=1.0),
        )
        with pytest.raises(BitfinexRateLimited) as exc_info:
            await client.get_funding_stats(symbol="fUSD", end=0, limit=10)
        assert exc_info.value.retry_after_seconds == 8.0


@pytest.mark.asyncio
async def test_get_funding_stats_500_raises_api_error(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="https://api-pub.bitfinex.com/v2/funding/stats/fUSD/hist?limit=10&end=0",
        status_code=500,
        text="internal error",
    )

    async with httpx.AsyncClient() as http:
        client = BitfinexREST(
            http=http,
            base_url="https://api-pub.bitfinex.com",
            limiter=FundingRateLimiter(max_rate=1000, time_period=1.0),
        )
        with pytest.raises(BitfinexAPIError) as exc_info:
            await client.get_funding_stats(symbol="fUSD", end=0, limit=10)
        assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_get_funding_stats_unexpected_shape_raises_shape_error(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="https://api-pub.bitfinex.com/v2/funding/stats/fUSD/hist?limit=10&end=0",
        json={"unexpected": "object instead of array"},
    )

    async with httpx.AsyncClient() as http:
        client = BitfinexREST(
            http=http,
            base_url="https://api-pub.bitfinex.com",
            limiter=FundingRateLimiter(max_rate=1000, time_period=1.0),
        )
        with pytest.raises(BitfinexShapeError):
            await client.get_funding_stats(symbol="fUSD", end=0, limit=10)
```

也在檔頭加 import：
```python
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat  # noqa: F401
```
（這 import 暫時可能用不到；保留是為了後續 test 擴增方便 — 若 ruff 抱怨就移除）

- [ ] **Step 3.2: Run new tests — expect import / attribute error**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_rest.py -v -k funding_stats`
Expected: 5 new tests FAIL — `AttributeError: BitfinexREST has no attribute 'get_funding_stats'` 或類似。

- [ ] **Step 3.3: Add `get_funding_stats` method to BitfinexREST**

修改 `backend_py/src/bfx_funding_bot/external/bitfinex/rest.py`：

1. 在檔頭 import 加：
```python
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
```

2. 在 `BitfinexREST` class 內 `get_funding_candles` 之後加新方法：

```python
    async def get_funding_stats(
        self,
        *,
        symbol: str,
        end: int,
        limit: int = 10000,
    ) -> list[FundingStat]:
        """Pull funding_stats rows.

        Endpoint: GET /v2/funding/stats/{symbol}/hist?limit=N&end=MS

        Returns rows in **descending** mts order (newest first), per Bitfinex.
        Pagination: max 10000 per call. `end` in **ms**, inclusive upper bound.

        Args:
            symbol: e.g. "fUSD", "fUST". Tolerates leading "f" already in
                place (path uses symbol as-is).
            end: ms timestamp inclusive upper bound.
            limit: max rows, max 10000.
        """
        sym = symbol if symbol.startswith("f") else f"f{symbol}"
        path = f"/v2/funding/stats/{sym}/hist"
        params = {"limit": limit, "end": end}

        async with self._limiter.acquire():
            try:
                resp = await self._http.get(
                    f"{self._base_url}{path}", params=params, timeout=30.0
                )
            except httpx.HTTPError as e:
                raise BitfinexAPIError(
                    status_code=0, message=f"transport error: {e}", raw=None
                ) from e

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            try:
                retry_after_seconds = float(retry_after) if retry_after else None
            except ValueError:
                retry_after_seconds = None
            raise BitfinexRateLimited(retry_after_seconds=retry_after_seconds)

        if resp.status_code >= 400:
            raise BitfinexAPIError(
                status_code=resp.status_code,
                message=resp.reason_phrase or "http error",
                raw=resp.text,
            )

        try:
            payload = resp.json()
        except json.JSONDecodeError as e:
            raise BitfinexShapeError(f"invalid JSON: {e}") from e

        if not isinstance(payload, list):
            raise BitfinexShapeError(
                f"expected list of funding_stats rows, "
                f"got {type(payload).__name__}: {payload!r}"
            )

        stats: list[FundingStat] = []
        for entry in payload:
            if not isinstance(entry, list):
                raise BitfinexShapeError(
                    f"expected each row to be a list, got {type(entry).__name__}"
                )
            try:
                stats.append(FundingStat.from_bitfinex(entry, symbol=sym))
            except (ValueError, TypeError) as e:
                raise BitfinexShapeError(
                    f"funding_stats parse failed: {e}; raw={entry!r}"
                ) from e

        return stats
```

- [ ] **Step 3.4: Run all bitfinex/test_rest.py tests — expect pass**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_rest.py -v`
Expected: 既有 4 tests + 5 new tests + 1 integration（skip）= 9 PASSED, 1 skipped。

- [ ] **Step 3.5: Run full suite + mypy + ruff**

Run:
```bash
cd backend_py && uv run pytest -m "not integration" -v && \
  uv run mypy src/ && uv run ruff check
```
Expected: 全綠。

- [ ] **Step 3.6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/external/bitfinex/rest.py \
        backend_py/tests/external/bitfinex/test_rest.py
git commit -m "✨ Feat: BitfinexREST.get_funding_stats

GET /v2/funding/stats/{symbol}/hist?limit=&end= → list[FundingStat]
Symmetric to get_funding_candles (limiter, error mapping, shape validation).
5 unit tests covering happy path / 429 / 500 / shape error / symbol prefix."
```

---

## Commit 4 — ✨ Feat: walking-back service for candles + funding_stats

**Goal:** 在兩個 service 加 `backfill_*_to_earliest`，含 resume-from-DB（讀 `get_min_mts` 推進 cursor）+ empty-page / `< limit` 終止 + cursor-stuck guard。新建 `modules/backfill/` 共用 schemas / errors。

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/backfill/__init__.py`
- Create: `backend_py/src/bfx_funding_bot/modules/backfill/schemas.py`
- Create: `backend_py/src/bfx_funding_bot/modules/backfill/errors.py`
- Create: `backend_py/tests/modules/backfill/__init__.py`
- Create: `backend_py/src/bfx_funding_bot/modules/funding_stats/service.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/candles/repository.py` (+get_min_mts)
- Modify: `backend_py/src/bfx_funding_bot/modules/candles/service.py` (+backfill_candles_to_earliest)
- Create: `backend_py/tests/modules/candles/test_walking_back.py`
- Create: `backend_py/tests/modules/funding_stats/test_walking_back.py`

### Tasks

- [ ] **Step 4.1: Create `modules/backfill/` skeleton**

```python
# backend_py/src/bfx_funding_bot/modules/backfill/__init__.py
```
（空檔。）

```python
# backend_py/src/bfx_funding_bot/modules/backfill/schemas.py
"""Shared schemas for Phase 2 backfill orchestration."""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SeriesSpec:
    """Identifies one backfill series.

    For kind="candles": (symbol, timeframe, period_agg) all required.
    For kind="funding_stats": only symbol used.
    """
    kind: Literal["candles", "funding_stats"]
    symbol: str
    timeframe: str | None = None
    period_agg: str | None = None

    def label(self) -> str:
        if self.kind == "candles":
            return f"candles {self.symbol} {self.timeframe} {self.period_agg}"
        return f"funding_stats {self.symbol}"


@dataclass(frozen=True)
class BackfillStats:
    """Successful backfill result for one series."""
    spec: SeriesSpec
    pages: int
    rows: int           # total rows upserted (may include resume-from-DB duplicates)
    earliest_mts: int   # smallest mts now in DB for this series after run


@dataclass(frozen=True)
class BackfillError:
    """Failed backfill for one series — orchestrator continues with others."""
    spec: SeriesSpec
    error: str          # repr / brief summary of exception
```

```python
# backend_py/src/bfx_funding_bot/modules/backfill/errors.py
"""Exceptions raised inside walking-back service loops."""


class BackfillCursorStuck(Exception):
    """Walking-back loop's cursor failed to advance.

    Bitfinex returned a page whose oldest mts is >= our current end_ms,
    which would cause an infinite loop. Most likely cause: API behaviour
    changed (no longer strictly < end_ms). Service raises this rather
    than silently looping.
    """

    def __init__(self, symbol: str, end_ms: int, oldest_mts: int) -> None:
        super().__init__(
            f"cursor stuck: symbol={symbol} end_ms={end_ms} "
            f"oldest_mts={oldest_mts} (expected oldest < end)"
        )
        self.symbol = symbol
        self.end_ms = end_ms
        self.oldest_mts = oldest_mts
```

```python
# backend_py/tests/modules/backfill/__init__.py
```
（空檔。）

- [ ] **Step 4.2: Write failing test for candles `get_min_mts`**

把以下測試**追加**到 `backend_py/tests/modules/candles/test_repository.py` 末尾：

```python
from bfx_funding_bot.modules.candles.repository import get_min_mts


@pytest.mark.asyncio
async def test_get_min_mts_returns_none_for_empty(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    result = await get_min_mts(
        sqlite_session, symbol="fUST", timeframe="1h", period_agg="p2",
    )
    assert result is None


@pytest.mark.asyncio
async def test_get_min_mts_returns_smallest(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    candles = [
        FundingCandle(
            symbol="fUST", timeframe="1h", period_agg="p2",
            mts=mts, open=Decimal("0.0001"), close=Decimal("0.0001"),
            high=Decimal("0.0001"), low=Decimal("0.0001"),
            volume=Decimal("100"),
        )
        for mts in [1700003600000, 1700000000000, 1700007200000]
    ]
    # different (timeframe, period_agg) — must not contaminate min
    candles.append(FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p30",
        mts=1500000000000, open=None, close=None, high=None, low=None, volume=None,
    ))

    await upsert_candles(sqlite_session, candles)
    await sqlite_session.commit()

    assert await get_min_mts(
        sqlite_session, symbol="fUST", timeframe="1h", period_agg="p2",
    ) == 1700000000000
    assert await get_min_mts(
        sqlite_session, symbol="fUST", timeframe="1h", period_agg="p30",
    ) == 1500000000000
```

- [ ] **Step 4.3: Run failing test**

Run: `cd backend_py && uv run pytest tests/modules/candles/test_repository.py::test_get_min_mts_returns_none_for_empty -v`
Expected: FAIL — `ImportError: cannot import name 'get_min_mts'`

- [ ] **Step 4.4: Implement `get_min_mts` in candles repository**

修改 `backend_py/src/bfx_funding_bot/modules/candles/repository.py`，在檔尾加：

```python
async def get_min_mts(
    session: AsyncSession,
    *,
    symbol: str,
    timeframe: str,
    period_agg: str,
) -> int | None:
    """Return smallest mts for the given (symbol, timeframe, period_agg) series,
    or None if no rows exist."""
    from sqlalchemy import func

    stmt = select(func.min(FundingCandleRow.mts)).where(
        FundingCandleRow.symbol == symbol,
        FundingCandleRow.timeframe == timeframe,
        FundingCandleRow.period_agg == period_agg,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
```

- [ ] **Step 4.5: Run candles repo tests — expect pass**

Run: `cd backend_py && uv run pytest tests/modules/candles/test_repository.py -v`
Expected: 既有 3 + 新 2 = 5 PASSED。

- [ ] **Step 4.6: Write failing test for `backfill_candles_to_earliest`**

Create: `backend_py/tests/modules/candles/test_walking_back.py`

```python
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.backfill.errors import BackfillCursorStuck
from bfx_funding_bot.modules.backfill.schemas import SeriesSpec
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import backfill_candles_to_earliest


@pytest.fixture
async def setup_schema(sqlite_engine: AsyncEngine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _candle(mts: int) -> FundingCandle:
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2",
        mts=mts,
        open=Decimal("0.0001"), close=Decimal("0.0001"),
        high=Decimal("0.0001"), low=Decimal("0.0001"),
        volume=Decimal("100"),
    )


@pytest.mark.asyncio
async def test_walks_back_until_empty_page(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """Two pages of 3 candles each, then empty → loop terminates after page 3."""
    page1 = [_candle(1700007200000), _candle(1700003600000), _candle(1700000000000)]
    page2 = [_candle(1699996400000), _candle(1699992800000), _candle(1699989200000)]
    pages = [page1, page2, []]

    mock_client: Any = AsyncMock()
    mock_client.get_funding_candles.side_effect = pages

    spec = SeriesSpec(kind="candles", symbol="fUST", timeframe="1h", period_agg="p2")
    stats = await backfill_candles_to_earliest(
        client=mock_client, session=sqlite_session,
        symbol="fUST", timeframe="1h", period_agg="p2",
        page_limit=3,
    )
    await sqlite_session.commit()

    assert stats.spec == spec
    assert stats.pages == 2     # page 3 was empty, didn't count
    assert stats.rows == 6
    assert stats.earliest_mts == 1699989200000
    assert mock_client.get_funding_candles.await_count == 3


@pytest.mark.asyncio
async def test_walks_back_terminates_on_partial_page(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """First page is full, second is half-full → second triggers early stop."""
    page1 = [_candle(1700007200000), _candle(1700003600000), _candle(1700000000000)]
    page2 = [_candle(1699996400000)]   # < limit
    mock_client: Any = AsyncMock()
    mock_client.get_funding_candles.side_effect = [page1, page2]

    stats = await backfill_candles_to_earliest(
        client=mock_client, session=sqlite_session,
        symbol="fUST", timeframe="1h", period_agg="p2",
        page_limit=3,
    )
    await sqlite_session.commit()

    assert stats.pages == 2
    assert stats.rows == 4
    assert mock_client.get_funding_candles.await_count == 2  # no 3rd call


@pytest.mark.asyncio
async def test_resumes_from_db_min_mts(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """When DB already has data, cursor starts at db_min_mts - 1, not now."""
    from bfx_funding_bot.modules.candles.repository import upsert_candles

    # Seed DB with two existing rows
    seeded = [_candle(1700007200000), _candle(1700003600000)]
    await upsert_candles(sqlite_session, seeded)
    await sqlite_session.commit()

    # Mock returns empty (already at earliest from resume cursor's POV)
    mock_client: Any = AsyncMock()
    mock_client.get_funding_candles.return_value = []

    await backfill_candles_to_earliest(
        client=mock_client, session=sqlite_session,
        symbol="fUST", timeframe="1h", period_agg="p2",
        page_limit=10,
    )

    # First call should have end = 1700003600000 - 1 (smallest existing - 1)
    call_kwargs = mock_client.get_funding_candles.await_args_list[0].kwargs
    assert call_kwargs["end"] == 1700003600000 - 1


@pytest.mark.asyncio
async def test_walks_back_starts_from_now_when_db_empty(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """Empty DB → first page's end is roughly now_ms (verify ≥ 1.7e12 i.e. > 2023)."""
    mock_client: Any = AsyncMock()
    mock_client.get_funding_candles.return_value = []

    await backfill_candles_to_earliest(
        client=mock_client, session=sqlite_session,
        symbol="fUST", timeframe="1h", period_agg="p2",
        page_limit=10,
    )

    call_kwargs = mock_client.get_funding_candles.await_args_list[0].kwargs
    assert call_kwargs["end"] > 1_700_000_000_000   # > 2023-11-15 ms


@pytest.mark.asyncio
async def test_raises_when_cursor_stuck(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """If Bitfinex returns a row whose mts >= end_ms, raise BackfillCursorStuck."""
    # Force start from end_ms = 1700000000000 by seeding one row at 1700000000001
    from bfx_funding_bot.modules.candles.repository import upsert_candles

    await upsert_candles(sqlite_session, [_candle(1700000000001)])
    await sqlite_session.commit()
    # Now resume cursor = 1700000000001 - 1 = 1700000000000

    # Mock returns a row whose mts == 1700000000000 (NOT strictly less)
    bad_page = [_candle(1700000000000)]
    mock_client: Any = AsyncMock()
    mock_client.get_funding_candles.return_value = bad_page

    with pytest.raises(BackfillCursorStuck):
        await backfill_candles_to_earliest(
            client=mock_client, session=sqlite_session,
            symbol="fUST", timeframe="1h", period_agg="p2",
            page_limit=10,
        )
```

- [ ] **Step 4.7: Run failing tests**

Run: `cd backend_py && uv run pytest tests/modules/candles/test_walking_back.py -v`
Expected: 5 tests FAIL — `ImportError: cannot import name 'backfill_candles_to_earliest'`

- [ ] **Step 4.8: Implement `backfill_candles_to_earliest`**

修改 `backend_py/src/bfx_funding_bot/modules/candles/service.py`，在檔尾加：

```python
import time

from bfx_funding_bot.modules.backfill.errors import BackfillCursorStuck
from bfx_funding_bot.modules.backfill.schemas import BackfillStats, SeriesSpec
from bfx_funding_bot.modules.candles.repository import get_min_mts


async def backfill_candles_to_earliest(
    *,
    client: BitfinexREST,
    session: AsyncSession,
    symbol: str,
    timeframe: str,
    period_agg: str,
    page_limit: int = 10000,
) -> BackfillStats:
    """Walking-back backfill until Bitfinex returns no more older candles.

    Resume-aware: if DB already has rows for this series, starts from
    (min_mts - 1) instead of now_ms. Caller manages session commit/rollback.
    Each batch is flushed (not committed) so subsequent get_min_mts sees it.
    """
    db_min = await get_min_mts(
        session, symbol=symbol, timeframe=timeframe, period_agg=period_agg,
    )
    end_ms = (db_min - 1) if db_min is not None else int(time.time() * 1000)

    pages = 0
    rows = 0
    while True:
        page = await client.get_funding_candles(
            symbol=symbol,
            timeframe=timeframe,
            period_agg=period_agg,
            start=0,
            end=end_ms,
            limit=page_limit,
        )
        if not page:
            break

        await upsert_candles(session, page)
        await session.flush()
        pages += 1
        rows += len(page)

        oldest_mts = min(c.mts for c in page)
        if oldest_mts >= end_ms:
            raise BackfillCursorStuck(symbol, end_ms, oldest_mts)
        end_ms = oldest_mts - 1

        if len(page) < page_limit:
            break

    final_min = await get_min_mts(
        session, symbol=symbol, timeframe=timeframe, period_agg=period_agg,
    )
    return BackfillStats(
        spec=SeriesSpec(
            kind="candles", symbol=symbol,
            timeframe=timeframe, period_agg=period_agg,
        ),
        pages=pages,
        rows=rows,
        earliest_mts=final_min if final_min is not None else end_ms,
    )
```

- [ ] **Step 4.9: Run candles walking-back tests**

Run: `cd backend_py && uv run pytest tests/modules/candles/test_walking_back.py -v`
Expected: 5 PASSED

- [ ] **Step 4.10: Write failing tests for funding_stats walking-back**

Create: `backend_py/tests/modules/funding_stats/test_walking_back.py`

```python
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.backfill.errors import BackfillCursorStuck
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from bfx_funding_bot.modules.funding_stats.service import (
    backfill_funding_stats_to_earliest,
)


@pytest.fixture
async def setup_schema(sqlite_engine: AsyncEngine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _stat(mts: int) -> FundingStat:
    return FundingStat(
        symbol="fUSD", mts=mts,
        frr=Decimal("5.8e-7"),
        avg_period=Decimal("2.3"),
        funding_amount=Decimal("4.5e7"),
        funding_amount_used=Decimal("2.1e7"),
        funding_below_threshold=Decimal("1.2e6"),
    )


@pytest.mark.asyncio
async def test_funding_stats_walks_back_until_empty(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    page1 = [_stat(1700007200000), _stat(1700003600000), _stat(1700000000000)]
    page2 = [_stat(1699996400000), _stat(1699992800000), _stat(1699989200000)]
    pages = [page1, page2, []]

    mock_client: Any = AsyncMock()
    mock_client.get_funding_stats.side_effect = pages

    stats = await backfill_funding_stats_to_earliest(
        client=mock_client, session=sqlite_session,
        symbol="fUSD", page_limit=3,
    )
    await sqlite_session.commit()

    assert stats.spec.kind == "funding_stats"
    assert stats.spec.symbol == "fUSD"
    assert stats.pages == 2
    assert stats.rows == 6
    assert stats.earliest_mts == 1699989200000


@pytest.mark.asyncio
async def test_funding_stats_resumes_from_db_min_mts(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    from bfx_funding_bot.modules.funding_stats.repository import upsert_funding_stats

    await upsert_funding_stats(sqlite_session, [_stat(1700003600000)])
    await sqlite_session.commit()

    mock_client: Any = AsyncMock()
    mock_client.get_funding_stats.return_value = []

    await backfill_funding_stats_to_earliest(
        client=mock_client, session=sqlite_session,
        symbol="fUSD", page_limit=10,
    )

    call_kwargs = mock_client.get_funding_stats.await_args_list[0].kwargs
    assert call_kwargs["end"] == 1700003600000 - 1


@pytest.mark.asyncio
async def test_funding_stats_terminates_on_partial_page(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    page1 = [_stat(1700007200000), _stat(1700003600000), _stat(1700000000000)]
    page2 = [_stat(1699996400000)]
    mock_client: Any = AsyncMock()
    mock_client.get_funding_stats.side_effect = [page1, page2]

    stats = await backfill_funding_stats_to_earliest(
        client=mock_client, session=sqlite_session,
        symbol="fUSD", page_limit=3,
    )
    await sqlite_session.commit()

    assert stats.pages == 2
    assert stats.rows == 4
    assert mock_client.get_funding_stats.await_count == 2


@pytest.mark.asyncio
async def test_funding_stats_raises_when_cursor_stuck(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    from bfx_funding_bot.modules.funding_stats.repository import upsert_funding_stats

    await upsert_funding_stats(sqlite_session, [_stat(1700000000001)])
    await sqlite_session.commit()

    bad_page = [_stat(1700000000000)]   # mts == end_ms after resume
    mock_client: Any = AsyncMock()
    mock_client.get_funding_stats.return_value = bad_page

    with pytest.raises(BackfillCursorStuck):
        await backfill_funding_stats_to_earliest(
            client=mock_client, session=sqlite_session,
            symbol="fUSD", page_limit=10,
        )
```

- [ ] **Step 4.11: Run failing tests**

Run: `cd backend_py && uv run pytest tests/modules/funding_stats/test_walking_back.py -v`
Expected: 4 tests FAIL — `ImportError`.

- [ ] **Step 4.12: Implement funding_stats service**

Create: `backend_py/src/bfx_funding_bot/modules/funding_stats/service.py`

```python
import time

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.modules.backfill.errors import BackfillCursorStuck
from bfx_funding_bot.modules.backfill.schemas import BackfillStats, SeriesSpec
from bfx_funding_bot.modules.funding_stats.repository import (
    get_min_mts,
    upsert_funding_stats,
)


async def backfill_funding_stats_to_earliest(
    *,
    client: BitfinexREST,
    session: AsyncSession,
    symbol: str,
    page_limit: int = 10000,
) -> BackfillStats:
    """Walking-back backfill of funding_stats until Bitfinex returns empty.

    Resume-aware: if DB already has rows for this symbol, starts from
    (min_mts - 1) instead of now_ms. Caller manages session commit/rollback.
    """
    db_min = await get_min_mts(session, symbol=symbol)
    end_ms = (db_min - 1) if db_min is not None else int(time.time() * 1000)

    pages = 0
    rows = 0
    while True:
        page = await client.get_funding_stats(
            symbol=symbol, end=end_ms, limit=page_limit,
        )
        if not page:
            break

        await upsert_funding_stats(session, page)
        await session.flush()
        pages += 1
        rows += len(page)

        oldest_mts = min(s.mts for s in page)
        if oldest_mts >= end_ms:
            raise BackfillCursorStuck(symbol, end_ms, oldest_mts)
        end_ms = oldest_mts - 1

        if len(page) < page_limit:
            break

    final_min = await get_min_mts(session, symbol=symbol)
    return BackfillStats(
        spec=SeriesSpec(kind="funding_stats", symbol=symbol),
        pages=pages,
        rows=rows,
        earliest_mts=final_min if final_min is not None else end_ms,
    )
```

- [ ] **Step 4.13: Run funding_stats walking-back tests**

Run: `cd backend_py && uv run pytest tests/modules/funding_stats/test_walking_back.py -v`
Expected: 4 PASSED

- [ ] **Step 4.14: Run full suite + mypy + ruff**

Run:
```bash
cd backend_py && uv run pytest -m "not integration" -v && \
  uv run mypy src/ && uv run ruff check
```
Expected: 全綠。

- [ ] **Step 4.15: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backfill/ \
        backend_py/src/bfx_funding_bot/modules/funding_stats/service.py \
        backend_py/src/bfx_funding_bot/modules/candles/repository.py \
        backend_py/src/bfx_funding_bot/modules/candles/service.py \
        backend_py/tests/modules/backfill/ \
        backend_py/tests/modules/candles/test_walking_back.py \
        backend_py/tests/modules/candles/test_repository.py \
        backend_py/tests/modules/funding_stats/test_walking_back.py
git commit -m "✨ Feat: walking-back service for candles + funding_stats

- modules/backfill: SeriesSpec, BackfillStats, BackfillError, BackfillCursorStuck
- candles.repository.get_min_mts (per series triple)
- candles.service.backfill_candles_to_earliest (walking-back + resume)
- funding_stats.service.backfill_funding_stats_to_earliest (parallel)
- 9 unit tests covering empty DB, resume, partial page, empty page, cursor-stuck"
```

---

## Commit 5 — ✨ Feat: scripts/backfill_phase2.py orchestrator + 4 PASS checks

**Goal:** 寫 `scripts/backfill_phase2.py` orchestrator：sequential 8 series、per-series session_scope、收集 results、跑 4 PASS check。把 4 個 check 抽成 `modules/backfill/checks.py` 純函式（可單元測），script 只負責 wiring。

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/backfill/checks.py`
- Create: `backend_py/tests/modules/backfill/test_checks.py`
- Create: `backend_py/scripts/backfill_phase2.py`

### Tasks

- [ ] **Step 5.1: Write failing tests for the 4 check functions**

Create: `backend_py/tests/modules/backfill/test_checks.py`

```python
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.backfill.checks import (
    check_continuity,
    check_frr_unit,
    check_round_trip,
    check_row_counts,
)
from bfx_funding_bot.modules.backfill.schemas import (
    BackfillError,
    BackfillStats,
    SeriesSpec,
)
from bfx_funding_bot.modules.candles.repository import upsert_candles
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.funding_stats.repository import upsert_funding_stats
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat


@pytest.fixture
async def setup_schema(sqlite_engine: AsyncEngine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _spec_candles(p: str = "p2") -> SeriesSpec:
    return SeriesSpec(kind="candles", symbol="fUST", timeframe="1h", period_agg=p)


def _spec_fs(symbol: str = "fUSD") -> SeriesSpec:
    return SeriesSpec(kind="funding_stats", symbol=symbol)


# ---------- check_row_counts ----------

@pytest.mark.asyncio
async def test_row_counts_passes_when_all_have_rows(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    await upsert_candles(sqlite_session, [
        FundingCandle(symbol="fUST", timeframe="1h", period_agg="p2",
                      mts=1700000000000, open=None, close=None,
                      high=None, low=None, volume=None),
    ])
    await upsert_funding_stats(sqlite_session, [
        FundingStat(symbol="fUSD", mts=1700000000000),
    ])
    await sqlite_session.commit()

    result = await check_row_counts(
        sqlite_session, [_spec_candles("p2"), _spec_fs("fUSD")]
    )
    assert result.passed is True
    assert result.failures == []


@pytest.mark.asyncio
async def test_row_counts_fails_for_empty_series(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    await upsert_candles(sqlite_session, [
        FundingCandle(symbol="fUST", timeframe="1h", period_agg="p2",
                      mts=1700000000000, open=None, close=None,
                      high=None, low=None, volume=None),
    ])
    await sqlite_session.commit()

    # specs include one empty series (p30) and one populated (p2)
    result = await check_row_counts(
        sqlite_session, [_spec_candles("p2"), _spec_candles("p30")]
    )
    assert result.passed is False
    assert len(result.failures) == 1
    assert "p30" in result.failures[0]


# ---------- check_round_trip ----------

@pytest.mark.asyncio
async def test_round_trip_passes_when_db_matches_fresh_fetch(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    candle = FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2",
        mts=1700000000000,
        open=Decimal("0.0001"), close=Decimal("0.0002"),
        high=Decimal("0.0003"), low=Decimal("0.00005"),
        volume=Decimal("100"),
    )
    await upsert_candles(sqlite_session, [candle])
    await sqlite_session.commit()

    mock_client: Any = AsyncMock()
    mock_client.get_funding_candles.return_value = [candle]
    mock_client.get_funding_stats.return_value = []

    result = await check_round_trip(
        sqlite_session, mock_client, [_spec_candles("p2")],
    )
    assert result.passed is True


@pytest.mark.asyncio
async def test_round_trip_fails_on_drift(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    stored = FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=1700000000000,
        open=Decimal("0.0001"), close=Decimal("0.0002"),
        high=Decimal("0.0003"), low=Decimal("0.00005"),
        volume=Decimal("100"),
    )
    await upsert_candles(sqlite_session, [stored])
    await sqlite_session.commit()

    drifted = stored.model_copy(update={"close": Decimal("0.0009")})
    mock_client: Any = AsyncMock()
    mock_client.get_funding_candles.return_value = [drifted]

    result = await check_round_trip(
        sqlite_session, mock_client, [_spec_candles("p2")],
    )
    assert result.passed is False


# ---------- check_frr_unit ----------

@pytest.mark.asyncio
async def test_frr_unit_passes_when_ratio_in_range(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    """FRR=5.8e-7/sec × 86400 ≈ 5e-2/day; candle close ~ 1e-4/day-equivalent.
    Ratio = 5e-2 / 1e-4 = 500 — fails the [0.1, 10] band.
    Use plausible values: FRR=5.8e-7, candle close=5e-2 → ratio=1.0 (good)."""
    await upsert_funding_stats(sqlite_session, [FundingStat(
        symbol="fUSD", mts=1700000000000, frr=Decimal("5.8e-7"),
    )])
    await upsert_candles(sqlite_session, [FundingCandle(
        symbol="fUSD", timeframe="1h", period_agg="p2", mts=1700001000000,
        open=None, close=Decimal("5e-2"),
        high=None, low=None, volume=None,
    )])
    await sqlite_session.commit()

    result = await check_frr_unit(sqlite_session, symbol="fUSD")
    assert result.passed is True


@pytest.mark.asyncio
async def test_frr_unit_fails_when_ratio_out_of_range(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    """FRR=5.8e-7 × 86400 = 5e-2; candle close = 1e-4. Ratio = 500 → fails."""
    await upsert_funding_stats(sqlite_session, [FundingStat(
        symbol="fUSD", mts=1700000000000, frr=Decimal("5.8e-7"),
    )])
    await upsert_candles(sqlite_session, [FundingCandle(
        symbol="fUSD", timeframe="1h", period_agg="p2", mts=1700001000000,
        open=None, close=Decimal("1e-4"),
        high=None, low=None, volume=None,
    )])
    await sqlite_session.commit()

    result = await check_frr_unit(sqlite_session, symbol="fUSD")
    assert result.passed is False
    assert "ratio" in result.message.lower()


@pytest.mark.asyncio
async def test_frr_unit_skips_when_no_data(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    """No funding_stats or no candles → check is skipped (not a failure)."""
    result = await check_frr_unit(sqlite_session, symbol="fUSD")
    assert result.passed is True   # skip = pass
    assert "skip" in result.message.lower() or "no data" in result.message.lower()


# ---------- check_continuity ----------

@pytest.mark.asyncio
async def test_continuity_passes_for_dense_candles(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    # 10 hourly candles → 100% continuity
    candles = [
        FundingCandle(
            symbol="fUST", timeframe="1h", period_agg="p2",
            mts=1700000000000 + i * 3_600_000,
            open=None, close=None, high=None, low=None, volume=None,
        )
        for i in range(10)
    ]
    await upsert_candles(sqlite_session, candles)
    await sqlite_session.commit()

    result = await check_continuity(sqlite_session, [_spec_candles("p2")])
    assert result.passed is True


@pytest.mark.asyncio
async def test_continuity_fails_when_gap_too_big(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    # 5 candles at hour 0,1,2 — then jumps to hour 100 (50% missing)
    candles = [
        FundingCandle(
            symbol="fUST", timeframe="1h", period_agg="p2",
            mts=mts, open=None, close=None, high=None, low=None, volume=None,
        )
        for mts in [
            1700000000000,
            1700003600000,
            1700007200000,
            1700000000000 + 100 * 3_600_000,
        ]
    ]
    await upsert_candles(sqlite_session, candles)
    await sqlite_session.commit()

    result = await check_continuity(sqlite_session, [_spec_candles("p2")])
    assert result.passed is False
```

- [ ] **Step 5.2: Run failing tests**

Run: `cd backend_py && uv run pytest tests/modules/backfill/test_checks.py -v`
Expected: ImportError — `cannot import name 'check_row_counts'`

- [ ] **Step 5.3: Implement `modules/backfill/checks.py`**

Create: `backend_py/src/bfx_funding_bot/modules/backfill/checks.py`

```python
"""4 PASS check functions for Phase 2 backfill orchestrator.

Each returns a CheckResult; orchestrator OR's `passed` and prints summary.
Pure-ish: takes a session + specs + (for round-trip) a Bitfinex client.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.modules.backfill.schemas import SeriesSpec
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.candles.tables import FundingCandleRow
from bfx_funding_bot.modules.funding_stats.repository import get_in_range
from bfx_funding_bot.modules.funding_stats.tables import FundingStatRow

CANDLE_INTERVAL_MS = 3_600_000          # 1h candles
MIN_CONTINUITY = Decimal("0.95")        # 95% threshold for candles
FRR_RATIO_LOW = Decimal("0.1")
FRR_RATIO_HIGH = Decimal("10")
FRR_SECONDS_PER_DAY = Decimal("86400")
ROUND_TRIP_TOLERANCE = Decimal("1e-15")


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    message: str
    failures: list[str] = field(default_factory=list)


# ---------- 1. row counts ----------

async def check_row_counts(
    session: AsyncSession, specs: list[SeriesSpec],
) -> CheckResult:
    """Every series must have row_count > 0."""
    failures: list[str] = []
    for spec in specs:
        if spec.kind == "candles":
            stmt = select(func.count()).select_from(FundingCandleRow).where(
                FundingCandleRow.symbol == spec.symbol,
                FundingCandleRow.timeframe == spec.timeframe,
                FundingCandleRow.period_agg == spec.period_agg,
            )
        else:
            stmt = select(func.count()).select_from(FundingStatRow).where(
                FundingStatRow.symbol == spec.symbol,
            )
        n = (await session.execute(stmt)).scalar_one()
        if n == 0:
            failures.append(f"{spec.label()} has 0 rows")

    return CheckResult(
        passed=not failures,
        message=f"row_count > 0 for {len(specs) - len(failures)}/{len(specs)} series",
        failures=failures,
    )


# ---------- 2. round-trip exact-match ----------

async def check_round_trip(
    session: AsyncSession,
    client: BitfinexREST,
    specs: list[SeriesSpec],
    sample_size: int = 10,
) -> CheckResult:
    """For up to one candle-spec and one funding_stats-spec, fetch from
    Bitfinex again and exact-match against DB."""
    failures: list[str] = []
    sampled = 0

    candle_specs = [s for s in specs if s.kind == "candles"]
    fs_specs = [s for s in specs if s.kind == "funding_stats"]

    if candle_specs:
        spec = candle_specs[0]
        assert spec.timeframe is not None and spec.period_agg is not None
        # Get newest mts in DB for this series
        stmt = select(func.max(FundingCandleRow.mts)).where(
            FundingCandleRow.symbol == spec.symbol,
            FundingCandleRow.timeframe == spec.timeframe,
            FundingCandleRow.period_agg == spec.period_agg,
        )
        max_mts = (await session.execute(stmt)).scalar_one_or_none()
        if max_mts is not None:
            fetched = await client.get_funding_candles(
                symbol=spec.symbol,
                timeframe=spec.timeframe,
                period_agg=spec.period_agg,
                start=0, end=max_mts, limit=sample_size,
            )
            if fetched:
                stored = await get_candles_in_range(
                    session,
                    symbol=spec.symbol,
                    timeframe=spec.timeframe,
                    period_agg=spec.period_agg,
                    start_mts=min(c.mts for c in fetched),
                    end_mts=max(c.mts for c in fetched),
                )
                stored_by_mts = {c.mts: c for c in stored}
                for f in fetched:
                    s = stored_by_mts.get(f.mts)
                    if s is None:
                        failures.append(f"{spec.label()} mts={f.mts} missing in DB")
                        continue
                    for fld in ("open", "close", "high", "low", "volume"):
                        fv = getattr(f, fld)
                        sv = getattr(s, fld)
                        if fv is None and sv is None:
                            continue
                        if fv is None or sv is None:
                            failures.append(
                                f"{spec.label()} mts={f.mts} {fld} nullness mismatch"
                            )
                            break
                        if abs(fv - sv) > ROUND_TRIP_TOLERANCE:
                            failures.append(
                                f"{spec.label()} mts={f.mts} {fld}: f={fv} != s={sv}"
                            )
                            break
                sampled += 1

    if fs_specs:
        spec = fs_specs[0]
        stmt2 = select(func.max(FundingStatRow.mts)).where(
            FundingStatRow.symbol == spec.symbol,
        )
        max_mts2 = (await session.execute(stmt2)).scalar_one_or_none()
        if max_mts2 is not None:
            fetched_fs = await client.get_funding_stats(
                symbol=spec.symbol, end=max_mts2, limit=sample_size,
            )
            if fetched_fs:
                stored_fs = await get_in_range(
                    session,
                    symbol=spec.symbol,
                    start_mts=min(s.mts for s in fetched_fs),
                    end_mts=max(s.mts for s in fetched_fs),
                )
                stored_fs_by_mts = {s.mts: s for s in stored_fs}
                for f in fetched_fs:
                    s = stored_fs_by_mts.get(f.mts)
                    if s is None:
                        failures.append(f"{spec.label()} mts={f.mts} missing in DB")
                        continue
                    for fld in (
                        "frr", "avg_period", "funding_amount",
                        "funding_amount_used", "funding_below_threshold",
                    ):
                        fv = getattr(f, fld)
                        sv = getattr(s, fld)
                        if fv is None and sv is None:
                            continue
                        if fv is None or sv is None:
                            failures.append(
                                f"{spec.label()} mts={f.mts} {fld} nullness mismatch"
                            )
                            break
                        if abs(fv - sv) > ROUND_TRIP_TOLERANCE:
                            failures.append(
                                f"{spec.label()} mts={f.mts} {fld}: f={fv} != s={sv}"
                            )
                            break
                sampled += 1

    return CheckResult(
        passed=not failures,
        message=f"round-trip exact-match: {sampled} series sampled",
        failures=failures,
    )


# ---------- 3. FRR unit sanity ----------

async def check_frr_unit(
    session: AsyncSession,
    symbol: str = "fUSD",
) -> CheckResult:
    """Take one funding_stats row, find a candle within ±30min, compare
    (frr × 86400) to candle close. Ratio should be in [0.1, 10] (one order
    of magnitude tolerance — this catches unit errors, not precision)."""
    fs_stmt = select(FundingStatRow).where(
        FundingStatRow.symbol == symbol, FundingStatRow.frr.is_not(None),
    ).limit(1)
    fs_row = (await session.execute(fs_stmt)).scalars().first()
    if fs_row is None or fs_row.frr is None:
        return CheckResult(passed=True, message="FRR unit check skipped: no funding_stats data")

    window_ms = 30 * 60 * 1000
    candle_stmt = (
        select(FundingCandleRow)
        .where(
            FundingCandleRow.symbol == symbol,
            FundingCandleRow.timeframe == "1h",
            FundingCandleRow.period_agg == "p2",
            FundingCandleRow.close.is_not(None),
            FundingCandleRow.mts >= fs_row.mts - window_ms,
            FundingCandleRow.mts <= fs_row.mts + window_ms,
        )
        .limit(1)
    )
    candle = (await session.execute(candle_stmt)).scalars().first()
    if candle is None or candle.close is None:
        return CheckResult(
            passed=True,
            message=f"FRR unit check skipped: no fUSD 1h p2 candle near mts={fs_row.mts}",
        )

    frr_dec = Decimal(str(fs_row.frr))
    close_dec = Decimal(str(candle.close))
    if close_dec == 0:
        return CheckResult(passed=True, message="FRR unit check skipped: candle close = 0")

    ratio = (frr_dec * FRR_SECONDS_PER_DAY) / close_dec
    if FRR_RATIO_LOW <= ratio <= FRR_RATIO_HIGH:
        return CheckResult(
            passed=True,
            message=(
                f"FRR unit sanity: frr × 86400 / candle.close = {ratio:.3f} "
                f"(in [{FRR_RATIO_LOW}, {FRR_RATIO_HIGH}]) → FRR is per-second"
            ),
        )
    return CheckResult(
        passed=False,
        message=(
            f"FRR unit mismatch: frr={frr_dec} × 86400 / candle.close={close_dec} "
            f"= ratio={ratio:.3f} (expected in [{FRR_RATIO_LOW}, {FRR_RATIO_HIGH}])"
        ),
        failures=[f"ratio out of range: {ratio:.3f}"],
    )


# ---------- 4. continuity ----------

async def check_continuity(
    session: AsyncSession, specs: list[SeriesSpec],
) -> CheckResult:
    """For candles: rows / expected_rows >= 0.95.
    For funding_stats: max consecutive gap < 1 day."""
    failures: list[str] = []

    for spec in specs:
        if spec.kind == "candles":
            assert spec.timeframe is not None and spec.period_agg is not None
            stmt = select(
                func.count(),
                func.min(FundingCandleRow.mts),
                func.max(FundingCandleRow.mts),
            ).where(
                FundingCandleRow.symbol == spec.symbol,
                FundingCandleRow.timeframe == spec.timeframe,
                FundingCandleRow.period_agg == spec.period_agg,
            )
            n, min_mts, max_mts = (await session.execute(stmt)).one()
            if n == 0 or min_mts is None or max_mts is None:
                continue   # row_count check covers this
            expected = (max_mts - min_mts) // CANDLE_INTERVAL_MS + 1
            if expected == 0:
                continue
            ratio = Decimal(n) / Decimal(expected)
            if ratio < MIN_CONTINUITY:
                failures.append(
                    f"{spec.label()}: {ratio:.2%} (rows={n}, expected≈{expected})"
                )
        else:
            stmt2 = select(FundingStatRow.mts).where(
                FundingStatRow.symbol == spec.symbol,
            ).order_by(FundingStatRow.mts.asc())
            mts_list = [r[0] for r in (await session.execute(stmt2)).all()]
            if len(mts_list) < 2:
                continue
            max_gap = max(
                mts_list[i + 1] - mts_list[i] for i in range(len(mts_list) - 1)
            )
            one_day_ms = 24 * 60 * 60 * 1000
            if max_gap > one_day_ms:
                failures.append(
                    f"{spec.label()}: max_gap = {max_gap / 3_600_000:.1f}h "
                    f"(threshold 24h)"
                )

    return CheckResult(
        passed=not failures,
        message=f"continuity: {len(specs) - len(failures)}/{len(specs)} series within threshold",
        failures=failures,
    )
```

- [ ] **Step 5.4: Run check tests — expect pass**

Run: `cd backend_py && uv run pytest tests/modules/backfill/test_checks.py -v`
Expected: 8 PASSED

- [ ] **Step 5.5: Write the orchestrator script**

Create: `backend_py/scripts/backfill_phase2.py`

```python
"""Phase 2 orchestrator: 8 series sequential backfill + 4 PASS checks.

Usage:
    cd backend_py
    uv run python scripts/backfill_phase2.py                # real run
    uv run python scripts/backfill_phase2.py --dry-run      # no API / no DB write

Series matrix (hardcoded):
    candles fUSD 1h p2 / p30 / a30
    candles fUST 1h p2 / p30 / a30
    funding_stats fUSD
    funding_stats fUST

Exit codes:
    0 — Phase 2 PASS (all 4 checks pass, all 8 series populated)
    1 — At least one PASS check failed
    2 — At least one series raised during backfill (others may have succeeded)
    3 — Operational error (network, DB connection, settings)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

import httpx
from sqlalchemy import text

from bfx_funding_bot.core.db import (
    make_engine,
    make_session_factory,
    session_scope,
)
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter
from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.modules.backfill.checks import (
    check_continuity,
    check_frr_unit,
    check_round_trip,
    check_row_counts,
)
from bfx_funding_bot.modules.backfill.schemas import (
    BackfillError,
    BackfillStats,
    SeriesSpec,
)
from bfx_funding_bot.modules.candles.service import backfill_candles_to_earliest
from bfx_funding_bot.modules.funding_stats.service import (
    backfill_funding_stats_to_earliest,
)

logger = logging.getLogger("backfill_phase2")

SERIES_MATRIX: list[SeriesSpec] = [
    SeriesSpec(kind="candles", symbol="fUSD", timeframe="1h", period_agg="p2"),
    SeriesSpec(kind="candles", symbol="fUSD", timeframe="1h", period_agg="p30"),
    SeriesSpec(kind="candles", symbol="fUSD", timeframe="1h", period_agg="a30"),
    SeriesSpec(kind="candles", symbol="fUST", timeframe="1h", period_agg="p2"),
    SeriesSpec(kind="candles", symbol="fUST", timeframe="1h", period_agg="p30"),
    SeriesSpec(kind="candles", symbol="fUST", timeframe="1h", period_agg="a30"),
    SeriesSpec(kind="funding_stats", symbol="fUSD"),
    SeriesSpec(kind="funding_stats", symbol="fUST"),
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run", action="store_true",
        help="Skip API calls and DB writes; verify settings + connectivity only.",
    )
    return p.parse_args()


async def _backfill_one(
    spec: SeriesSpec,
    client: BitfinexREST,
    session_factory,
) -> BackfillStats | BackfillError:
    """Run one series in its own session_scope; catch + record any exception."""
    try:
        async with session_scope(session_factory) as session:
            if spec.kind == "candles":
                assert spec.timeframe is not None and spec.period_agg is not None
                return await backfill_candles_to_earliest(
                    client=client, session=session,
                    symbol=spec.symbol,
                    timeframe=spec.timeframe,
                    period_agg=spec.period_agg,
                )
            return await backfill_funding_stats_to_earliest(
                client=client, session=session, symbol=spec.symbol,
            )
    except Exception as e:
        logger.exception("series %s failed", spec.label())
        return BackfillError(spec=spec, error=repr(e))


def _format_summary(results: list[BackfillStats | BackfillError]) -> str:
    lines = [
        "=== Phase 2 Backfill Summary ===",
        f"{'SERIES':<32} {'PAGES':>6} {'ROWS':>8} {'EARLIEST_MTS':>16} STATUS",
    ]
    for r in results:
        if isinstance(r, BackfillStats):
            lines.append(
                f"{r.spec.label():<32} {r.pages:>6} {r.rows:>8} "
                f"{r.earliest_mts:>16} ✓"
            )
        else:
            lines.append(f"{r.spec.label():<32} {'-':>6} {'-':>8} {'-':>16} ✗ {r.error}")
    return "\n".join(lines)


async def _amain() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    settings = Settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    if args.dry_run:
        async with session_scope(session_factory) as session:
            await session.execute(text("SELECT 1"))
        logger.info("✅ Dry-run PASS: settings + DB connectivity OK")
        await engine.dispose()
        return 0

    started = time.time()
    try:
        async with httpx.AsyncClient() as http:
            client = BitfinexREST(
                http=http,
                base_url=settings.bitfinex_api_base_url,
                limiter=FundingRateLimiter(),
            )
            results: list[BackfillStats | BackfillError] = []
            for spec in SERIES_MATRIX:
                logger.info("→ starting %s", spec.label())
                t0 = time.time()
                r = await _backfill_one(spec, client, session_factory)
                dt = time.time() - t0
                if isinstance(r, BackfillStats):
                    logger.info(
                        "← %s done: %d pages, %d rows in %.1fs",
                        spec.label(), r.pages, r.rows, dt,
                    )
                else:
                    logger.error("← %s FAILED: %s", spec.label(), r.error)
                results.append(r)

            print(_format_summary(results))
            print()

            # Run 4 PASS checks
            specs_ok = [r.spec for r in results if isinstance(r, BackfillStats)]

            async with session_scope(session_factory) as session:
                rc = await check_row_counts(session, [r.spec for r in results])
                rt = await check_round_trip(session, client, specs_ok)
                fr = await check_frr_unit(session, symbol="fUSD")
                ct = await check_continuity(session, specs_ok)

            print("=== PASS Checks ===")
            for label, res in [
                ("row_count > 0", rc),
                ("round-trip exact-match", rt),
                ("FRR unit sanity", fr),
                ("continuity", ct),
            ]:
                mark = "✓" if res.passed else "✗"
                print(f"[{mark}] {label}: {res.message}")
                for f in res.failures:
                    print(f"    - {f}")

            logger.info("Total elapsed: %.1fs", time.time() - started)

            checks_passed = rc.passed and rt.passed and fr.passed and ct.passed
            any_series_failed = any(isinstance(r, BackfillError) for r in results)

            if not checks_passed:
                print("\n❌ Phase 2 FAIL — see above")
                return 1
            if any_series_failed:
                print("\n⚠️ Phase 2 partial — checks pass but some series errored")
                return 2
            print("\n✅ Phase 2 PASS")
            return 0
    except Exception:
        logger.exception("Operational error during Phase 2 backfill")
        return 3
    finally:
        await engine.dispose()


def main() -> None:
    rc = asyncio.run(_amain())
    sys.exit(rc)


if __name__ == "__main__":
    main()
```

- [ ] **Step 5.6: Run script in dry-run to verify wiring**

Run:
```bash
cd backend_py && uv run python scripts/backfill_phase2.py --dry-run
```
Expected: log 顯示 `✅ Dry-run PASS: settings + DB connectivity OK`，exit 0。
若失敗：檢查 `.env` symlink、`Settings()` 是否能讀到 DATABASE_URL、Neon 是否在線。

- [ ] **Step 5.7: Run full test suite + mypy + ruff**

Run:
```bash
cd backend_py && uv run pytest -m "not integration" -v && \
  uv run mypy src/ scripts/ && uv run ruff check
```
Expected: 全綠（含 8 個新 backfill check tests）。

- [ ] **Step 5.8: Run real backfill against Neon — Phase 2 actual execution**

Run:
```bash
cd backend_py && uv run python scripts/backfill_phase2.py 2>&1 | tee /tmp/phase2-run.log
```
Expected:
- 約 20-30 分鐘 runtime（log 即時印每 series 進度）
- 印出 `=== Phase 2 Backfill Summary ===` 表格，8 series 全 ✓
- 印出 `=== PASS Checks ===`，全 ✓
- 結尾 `✅ Phase 2 PASS`，exit 0

若 4 個 check 中有 ✗：
- **Check 1 fail**（row_count = 0）→ 該 series 對 Bitfinex 不存在；確認 symbol/period_agg 無拼錯
- **Check 2 fail**（round-trip drift）→ upsert 邏輯有問題；查 `_to_float_or_none` 路徑
- **Check 3 fail**（FRR unit）→ 推測「FRR 是秒利率」是錯的；保留 log，**這不阻塞 commit**，但 Commit 6 wiki Lessons 要寫「FRR 單位待解」
- **Check 4 fail**（continuity）→ resume-from-DB 邊界 bug 或 Bitfinex 中段缺資料；查 log 看哪個 series、哪個區間

若 series 個別 fail（partial pass，exit 2）：
- 重跑同個指令；resume-from-DB 會接著走，failure 機率二次重跑通常會解
- 真不行就針對該 series 縮小範圍 debug

把 `/tmp/phase2-run.log` 留著（Commit 6 寫 result doc 用）。

- [ ] **Step 5.9: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backfill/checks.py \
        backend_py/tests/modules/backfill/test_checks.py \
        backend_py/scripts/backfill_phase2.py
git commit -m "✨ Feat: scripts/backfill_phase2.py orchestrator + 4 PASS checks

- modules/backfill/checks.py: row_counts / round_trip / frr_unit / continuity
- 8 unit tests for check functions (pure-ish, sqlite + AsyncMock)
- scripts/backfill_phase2.py: 8-series sequential orchestrator with --dry-run
- Exit codes: 0 PASS / 1 check fail / 2 series fail / 3 operational"
```

---

## Commit 6 — 📝 Docs: Phase 2 result + FRR unit confirmation

**Goal:** 把 Step 5.8 的真實 backfill 結果落 doc，更新 wiki 的 Pending → 完成、加 Lessons Learned 一行（含 FRR ratio 實測值）、Recent Activity 一條。

**Files:**
- Create: `docs/superpowers/specs/2026-05-10-phase2-result.md`
- Modify: `~/second-brain/wiki/projects/bfx-funding-bot.md`

### Tasks

- [ ] **Step 6.1: Write Phase 2 result doc**

從 `/tmp/phase2-run.log` 抽以下資訊填進 `docs/superpowers/specs/2026-05-10-phase2-result.md`：

```markdown
# v2 Phase 2 — Backfill Result

**Date**: 2026-05-10（or 實際執行日）
**Spec**: `docs/superpowers/specs/2026-05-10-phase2-data-backfill-design.md`
**Plan**: `docs/superpowers/plans/2026-05-10-phase2-data-backfill.md`
**Runtime**: <X> minutes（從 log 抓）
**Exit code**: 0（PASS）/ 1 / 2 / 3

## 8 Series Summary

| Series | Pages | Rows | Earliest mts | Earliest UTC |
|---|---|---|---|---|
| candles fUSD 1h p2 | <填> | <填> | <填> | <填> |
| candles fUSD 1h p30 | | | | |
| candles fUSD 1h a30 | | | | |
| candles fUST 1h p2 | | | | |
| candles fUST 1h p30 | | | | |
| candles fUST 1h a30 | | | | |
| funding_stats fUSD | | | | |
| funding_stats fUST | | | | |

Total rows: <填>

## 4 PASS Checks

- [✓/✗] row_count > 0 for all 8 series — <message>
- [✓/✗] round-trip exact-match — <message>
- [✓/✗] FRR unit sanity — frr × 86400 / candle.close = <ratio>（in [0.1, 10] → FRR 確認為秒利率 / 不在範圍 → 待解）
- [✓/✗] continuity — <message>

## FRR 單位確認

從 `check_frr_unit` 抓的實測值：
- 取樣 mts: <填>
- frr: <填>
- candle close: <填>
- ratio (frr × 86400 / close): <填>
- 結論：FRR 是 <每秒利率 / 其他>

5/9 推測「FRR=5.8e-7 是秒利率」<確認 / 推翻>。

## 對 Phase 3 的影響

- [若 FRR 單位 PASS] backtest engine `market_rate_source="frr"` 啟用時，從 `funding_stats.repository.get_frr_at_or_before_mts()` 抓 FRR，**乘 86400 換成日利率**再餵進 `_apply_friction`
- [若 FRR 單位 FAIL] Phase 3 不可貿然 swap；先在策略 brainstorm 中重新確認單位含義

## Notes / Issues encountered

<填：runtime 比預期長 / 短？哪些 series 第一次 fail 重跑才過？rate limit 是否觸發？>
```

**`<填>` 部分必須從實際 log 填入；不可留 `<填>` 在最終 commit。**

- [ ] **Step 6.2: Update wiki — bfx-funding-bot.md**

修改 `~/second-brain/wiki/projects/bfx-funding-bot.md`：

1. 在 **Pending → v2 Strategy Iteration** 段（line 79 附近），把：
   ```
   - [ ] **Phase 2**: 資料層回填 — 待 brainstorm
   ```
   改成：
   ```
   - [x] ~~**Phase 2**: 資料層回填~~ — 5/10 完成（spec / plan / 6 commits / Phase 2 result doc；Total <X> rows in <Y> minutes）
   ```

2. **Pending → FRR 單位確認** 條目（line 87 附近）：
   ```
   - [ ] **FRR 單位確認**（5/9 推測秒利率，Phase 2 補資料時驗證）
   ```
   改成 `- [x] ~~...~~ — Phase 2 confirmed: ratio = <X.XX>（FRR 是秒利率 / 待解）`

3. **Lessons Learned** 段加一行：
   ```
   - **FRR 單位**: 秒利率（× 86400 = 日利率）— Phase 2 backfill 抽樣驗證 ratio = <X.XX>（若 PASS）/ 待解（若 FAIL）
   ```

4. **Recent Activity → 2026-05-10**（或執行日）加一條：
   ```
   - **v2 Phase 2 資料層回填 PASS**（6 commits）
     - **Spec + plan + execute**：`docs/superpowers/specs/2026-05-10-phase2-data-backfill-design.md` + `docs/superpowers/plans/2026-05-10-phase2-data-backfill.md`
     - **Backfill 結果**：8 series（fUSD/fUST × 1h × p2/p30/a30 candles + 2 funding_stats）共 <X> rows，<Y> 分鐘
     - **FRR 單位**：ratio = <X.XX>，<確認秒利率 / 待解>
     - 為 Phase 3 6 候選策略 backtest 鋪好資料層；`get_frr_at_or_before_mts` API 已備好
   ```

- [ ] **Step 6.3: Commit (in bfx-funding-bot repo)**

```bash
git add docs/superpowers/specs/2026-05-10-phase2-result.md
git commit -m "📝 Docs: v2 Phase 2 result + FRR unit confirmation

- Total <X> rows backfilled across 8 series in <Y> minutes
- 4 PASS checks all green
- FRR unit confirmed: per-second (ratio = <X.XX>)  [or: pending]
- Phase 3 unblocked for FRR-trend / SpikeDetect / bid-relative-to-FRR strategies"
```

- [ ] **Step 6.4: Commit second-brain wiki separately**

second-brain 是另一個 repo（gitignored 父 repo / 子 repo 結構），按 second-brain CLAUDE.md 規則「一天一 commit」— 不在這個 phase 結束時 commit，等晚上 daily 一起：

```bash
# 不立即 commit；只更新檔案，留待 daily aggregation
cd ~/second-brain
git status   # 確認 wiki/projects/bfx-funding-bot.md 在 modified
```

---

## Verification（Phase 2 整體 PASS 條件）

完工檢查清單，逐項確認後才算 Phase 2 ship：

- [ ] **V1**: `cd backend_py && uv run pytest -m "not integration" -v` 全綠（既有 + 新增 Phase 2 tests）
- [ ] **V2**: `cd backend_py && uv run mypy src/ scripts/ && uv run ruff check` 無錯
- [ ] **V3**: `cd backend_py && uv run alembic check` 顯示 `No new upgrade operations detected.`（Commit 1 之後就應一直是這狀態）
- [ ] **V4**: `cd backend_py && uv run python scripts/backfill_phase2.py` 跑完 exit 0，4 PASS check 全 ✓
- [ ] **V5**: `docs/superpowers/specs/2026-05-10-phase2-result.md` 存在，`<填>` 都已填入實際數字
- [ ] **V6**: `~/second-brain/wiki/projects/bfx-funding-bot.md` 的 Phase 2 條目改成 `[x]`、Lessons Learned 加 FRR 單位、Recent Activity 加條目
- [ ] **V7**: `git log --oneline` 看到 6 個 Phase 2 commits（不算 spec/plan）順序正確

若 V4 的 Check 3（FRR unit）fail，仍可繼續 V5-V7（spec 已說明此情況不阻塞 ship）；但 Phase 3 brainstorm 必須先處理 FRR 單位含義。

---

## Self-Review notes（plan 完工自我檢查）

- ✅ Spec 每個 commit / Component 都對應到一個 Task 區段
- ✅ TDD 順序：每個 commit 先寫測試 → 跑失敗 → 寫實作 → 跑通過 → commit
- ✅ 每個 step 含實際 code（不留 TBD）
- ✅ Type / 命名一致：`backfill_candles_to_earliest` 與 `backfill_funding_stats_to_earliest` 一對一；`SeriesSpec` / `BackfillStats` / `BackfillError` / `BackfillCursorStuck` 命名橫貫 4 commits 一致
- ✅ 環境注意事項在 plan 開頭強調（cd backend_py、.env symlink、用 Alembic 不是 Atlas）
- ✅ Verification 4 大項涵蓋 spec PASS criteria + lint/mypy/migration/ship-doc
