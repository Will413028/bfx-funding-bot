# v2 Phase 2 — 資料層回填 Design

**Status**: design (pending implementation plan)
**Date**: 2026-05-10
**Owner**: Will（solo）
**Brainstorm session**: 2026-05-10
**Parent**: v2 strategy iteration phase（Phase 1 引擎升級已 ship；Phase 3 待後）

---

## Why

Phase 1 引擎升級用 candle close 當 FRR proxy（spec 註明 v2 簡化）。Phase 3 6 候選策略中至少有 3 個吃 FRR（FRR-trend、SpikeDetect、bid-relative-to-FRR），策略 A/B 必須有真 FRR 才有意義。

同時，在 backtest matrix 跑「2 symbols × 3 period_agg × 6 strategies」之前，全歷史 1h candle 必須完整 — 不然 walk-forward window 會被資料量限制住。Phase 2 把這兩塊資料層補齊。

## What

1. `funding_stats` 全模組（schemas / repository / service）對齊 candles 模組分層
2. `BitfinexREST.get_funding_stats()` 對應 `/v2/funding/stats/f{Symbol}/hist`
3. `service.backfill_to_earliest()`（candles + funding_stats 各一份）— walking-back + resume-from-DB
4. `scripts/backfill_phase2.py` 一支 orchestrator → 8 series sequential → 4 項 PASS check
5. `FundingStatRow` 從 `candles/tables.py` 搬到 `funding_stats/tables.py`（同 table 名，Atlas no-op）

## Out of Scope

| 項目 | 推遲到 |
|---|---|
| `market_rate_source="frr"` 在 backtest engine 啟用 | Phase 3（讀 API 已備好，只待 wire） |
| 增量 / 排程 sync（cron、worker、Redis lock） | Phase 3 完才解凍 12 deferred 之一 |
| 多 timeframe（5m / 1D） | v3+；本 phase 只 1h |
| 跨 exchange / 跨幣對（fBTC, fETH 等） | v3+；本 phase 限 fUSD/fUST |
| Bitfinex `funding/stats` 全 18 欄位 | 沿用既有 5 欄（FRR / AVG_PERIOD / FUNDING_AMOUNT / FUNDING_AMOUNT_USED / FUNDING_BELOW_THRESHOLD），其餘 placeholder 不存 |
| Backfill 進度 dashboard / Slack 通知 | YAGNI；orchestrator log 夠 |
| Backtest engine 任何修改 | Phase 3 才動 |

## Architecture

### Walking-back + resume-from-DB 演算法

candles 與 funding_stats 共用形狀，只差 series 的 key tuple（candles 多 timeframe、period_agg）。

```python
async def backfill_to_earliest(
    *, client, session, symbol, [timeframe, period_agg,]  # candles only
    page_limit: int = 10000,
) -> BackfillStats:
    # 1. 解析起點 end_ms
    db_min_mts = await repo.get_min_mts(session, symbol=..., ...)
    end_ms = (db_min_mts - 1) if db_min_mts is not None else now_ms()

    total_inserted = 0
    pages = 0
    while True:
        # 2. 拉一頁（DESC，最新 → 最舊）
        page = await client.get_X(symbol=..., end=end_ms, limit=page_limit)
        if not page:
            break  # auto-discover：抓到 Bitfinex 最早那筆之前

        await repo.upsert(session, page)
        await session.flush()  # 讓 min_mts 即時反映；commit 由 caller 控
        total_inserted += len(page)
        pages += 1

        # 3. 推進 cursor
        oldest_mts = min(c.mts for c in page)
        if oldest_mts >= end_ms:
            raise BackfillCursorStuck(symbol, end_ms, oldest_mts)
        end_ms = oldest_mts - 1

        # 4. 本頁不滿 limit 表示已到尾端
        if len(page) < page_limit:
            break

    return BackfillStats(...)
```

### 關鍵設計選擇

1. **`db_min_mts - 1` 作 resume cursor** — `mts` 是 ms timestamp，`- 1` 確保下一頁不會重抓同一筆（即使 Bitfinex 邊界 inclusive）。Resume 從上次最舊那筆之前繼續往前。
2. **Empty page → 終止** — 不需要查 Bitfinex 上線日；Bitfinex 對超出歷史的 `end` 會回 `[]`，這就是 auto-discover 的天然訊號。
3. **`len(page) < limit` → 終止** — 提早跳出，少打一次 API。
4. **`oldest_mts >= end_ms` → raise** — 防呆：理論上 Bitfinex 會回嚴格 `< end_ms` 的資料；萬一 API 行為改變，cursor 卡住會無限迴圈，此時 raise 而非沉默。
5. **Per-batch flush，per-series commit** — orchestrator 每個 series 包一個 `session_scope`；service 內 `flush()` 讓 `get_min_mts()` 反映已寫入的，但整 series 失敗能整段 rollback。
6. **Service 不關心 series matrix** — orchestrator 才知道「6 candle + 2 funding_stats」這事；service 只處理「給我一個 series，我把它走完」。

### Orchestrator 結構

```python
SERIES_MATRIX: list[SeriesSpec] = [
    SeriesSpec("candles", "fUSD", "1h", "p2"),
    SeriesSpec("candles", "fUSD", "1h", "p30"),
    SeriesSpec("candles", "fUSD", "1h", "a30"),
    SeriesSpec("candles", "fUST", "1h", "p2"),
    SeriesSpec("candles", "fUST", "1h", "p30"),
    SeriesSpec("candles", "fUST", "1h", "a30"),
    SeriesSpec("funding_stats", "fUSD"),
    SeriesSpec("funding_stats", "fUST"),
]

results: list[BackfillStats | BackfillError] = []
for spec in SERIES_MATRIX:
    async with session_scope(factory) as s:
        try:
            results.append(await dispatch_backfill(spec, client, s))
        except Exception as e:
            logger.exception("series %s failed", spec)
            results.append(BackfillError(spec, str(e)))
            # 不中斷其他 series

exit_code = run_pass_checks(results, session_factory)
```

`dispatch_backfill` 根據 `spec.kind` 路由到 `candles.service.backfill_candles_to_earliest` 或 `funding_stats.service.backfill_to_earliest`。

## Components

### 新增

#### `modules/funding_stats/tables.py`
- 把 `FundingStatRow` 從 `candles/tables.py` 搬過來，**不改 `__tablename__`**（仍 `funding_stats`）
- Atlas migrate diff 應該看到 no-op；保險起見實作時 `cd backend_py/schema && atlas migrate diff phase2_funding_stats_module --env neon` 跑一次確認

#### `modules/funding_stats/schemas.py` — 對齊 `candles/schemas.py` 風格

```python
class FundingStat(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    symbol: str = Field(min_length=1)
    mts: int
    frr: Decimal | None = None
    avg_period: Decimal | None = None
    funding_amount: Decimal | None = None
    funding_amount_used: Decimal | None = None
    funding_below_threshold: Decimal | None = None

    @field_validator("frr", "avg_period", "funding_amount",
                     "funding_amount_used", "funding_below_threshold", mode="before")
    @classmethod
    def _coerce_decimal(cls, v): return _to_decimal(v)

    def timestamp(self) -> datetime: ...

    @classmethod
    def from_bitfinex(cls, raw: list[Any], *, symbol: str) -> "FundingStat":
        # raw shape (per Bitfinex v2 docs, to be re-verified by curl in Commit 3):
        # [MTS, FRR, AVG_PERIOD, _, _, _, _, _, _, _,
        #  FUNDING_AMOUNT, FUNDING_AMOUNT_USED, _, _, _,
        #  FUNDING_BELOW_THRESHOLD]   (16 entries)
        if len(raw) < 16:
            raise ValueError(f"expected ≥16 elements, got {len(raw)}: {raw!r}")
        return cls(
            symbol=symbol, mts=int(raw[0]),
            frr=_to_decimal(raw[1]), avg_period=_to_decimal(raw[2]),
            funding_amount=_to_decimal(raw[10]),
            funding_amount_used=_to_decimal(raw[11]),
            funding_below_threshold=_to_decimal(raw[15]),
        )
```

#### `modules/funding_stats/repository.py`

```python
async def upsert_funding_stats(session, stats: list[FundingStat]) -> None:
    """ON CONFLICT (symbol, mts) DO UPDATE 全 5 欄"""

async def get_min_mts(session, *, symbol: str) -> int | None:
    """SELECT min(mts) WHERE symbol=:symbol；空表回 None"""

async def get_in_range(session, *, symbol, start_mts, end_mts) -> list[FundingStat]:
    """ASC by mts"""

async def get_frr_at_or_before_mts(session, *, symbol, mts) -> FundingStat | None:
    """SELECT ... WHERE symbol=:s AND mts <= :mts ORDER BY mts DESC LIMIT 1
    Phase 3 backtest engine wire 這個（每 candle 對齊一筆 FRR）。"""
```

#### `modules/funding_stats/service.py`

```python
async def backfill_to_earliest(
    *, client: BitfinexREST, session: AsyncSession,
    symbol: str, page_limit: int = 10000,
) -> BackfillStats: ...
```

#### `modules/backfill/schemas.py`（新模組）

```python
@dataclass(frozen=True)
class SeriesSpec:
    kind: Literal["candles", "funding_stats"]
    symbol: str
    timeframe: str | None = None    # candles only
    period_agg: str | None = None   # candles only

@dataclass(frozen=True)
class BackfillStats:
    spec: SeriesSpec
    pages: int
    rows: int
    end_mts: int        # 最後一筆 cursor 落點

@dataclass(frozen=True)
class BackfillError:
    spec: SeriesSpec
    error: str          # exception 摘要
```

#### `modules/backfill/errors.py`（新模組）

```python
class BackfillCursorStuck(Exception):
    """Walking-back loop 的 cursor 沒推進 — Bitfinex API 回了不嚴格 < end_ms 的資料。
    防止 oldest_mts >= end_ms 時的無限迴圈；service.backfill_to_earliest raise 此例外。"""
    def __init__(self, symbol: str, end_ms: int, oldest_mts: int): ...
```

#### `scripts/backfill_phase2.py`（orchestrator）

```python
SERIES_MATRIX: list[SeriesSpec] = [...8 條...]

async def _amain() -> int:
    # 1. settings, engine, session_factory, http, client（同 backfill_candles.py 套路）
    # 2. for spec in SERIES_MATRIX: 各包 session_scope、catch exception 收進 results
    # 3. 4 項 PASS check（見 Verification）
    # 4. log summary 表格 + exit code
    # exit codes: 0 PASS / 1 PASS-check fail / 2 series fail / 3 operational
```

支援 `--dry-run`：不打 API、不寫 DB，只跑 schema / connectivity check 後即收尾。

### 修改

#### `external/bitfinex/rest.py` — 加 `get_funding_stats()`

```python
async def get_funding_stats(
    self, *, symbol: str, end: int, limit: int = 10000,
) -> list[FundingStat]:
    """GET /v2/funding/stats/f{symbol}/hist?end=&limit="""
```

對稱於 `get_funding_candles`：同樣走 limiter / 同樣的 429 / shape error 路徑。

#### `modules/candles/repository.py`

加：
```python
async def get_min_mts(session, *, symbol, timeframe, period_agg) -> int | None
```

#### `modules/candles/service.py`

加：
```python
async def backfill_candles_to_earliest(
    *, client, session, symbol, timeframe, period_agg, page_limit: int = 10000,
) -> BackfillStats
```

既有 `backfill_candles()` single-window primitive **不動**（Checkpoint 1 driver `scripts/backfill_candles.py` 繼續用）。

#### `modules/candles/tables.py`
- 移除 `FundingStatRow`
- import 路徑改：`tests/modules/candles/test_tables.py` → 搬到 `tests/modules/funding_stats/test_tables.py`

#### `core/db.py` 或 `__init__.py`
- 確保 funding_stats `tables.py` 被 import，讓 `Base.metadata` 掃到（具體 import 點看 candles 怎麼接，照辦）

## Verification（Phase 2 PASS 條件）

Orchestrator 跑完 8 series（含失敗的）後依序跑 4 項 — 任一失敗 → exit 1，全過 → exit 0。

### Check 1 — 8 series 全部 row_count > 0

```python
for spec in SERIES_MATRIX:
    n = await repo.count_rows(session, spec)
    if n == 0: FAIL(f"{spec} has 0 rows")
```

失敗例：Bitfinex 該 series 不存在（symbol 拼錯、period_agg 沒上線）、orchestrator catch 但 series 一筆都沒寫成功。

### Check 2 — Spot-check round-trip exact-match

- 從 results 取 2 條成功的 series（1 candle + 1 funding_stats）
- 對每條，重新 `client.get_X(end=最新一筆 mts, limit=10)` 抓 10 筆
- 與 DB 同 mts 區間 read back，欄位 exact-match（`abs(diff) < 1e-15`，沿用 `backfill_candles.py` 既有判斷）

失敗例：upsert 某欄掉精度、`from_bitfinex` 解析 array 位置錯位。

### Check 3 — FRR 單位 sanity check

```python
fs = await repo.get_in_range(session, symbol="fUSD", start, end)  # 取一筆
candle = await candle_repo.get_in_range(...)  # 同 mts ±30min 的 fUSD 1h p2
ratio = (fs.frr * Decimal("86400")) / candle.close
if not (Decimal("0.1") <= ratio <= Decimal("10")):
    FAIL(f"FRR unit mismatch: ratio×86400={ratio}")
```

- 通過：證實 FRR 是秒利率（× 86400 = 日利率）
- 失敗：FRR 單位另有他解 → spec 在 Risk 段標 Phase 3 阻塞、寫 wiki Lessons、暫不 swap engine
- 容忍區間 `[0.1, 10]×`（一個 order of magnitude）刻意鬆 — 這 check 抓的是「差兩三個數量級」級別的單位錯誤

### Check 4 — Continuity check（無中間空段）

對 candles：
```python
expected_rows = (max_mts - min_mts) // 3_600_000 + 1   # 1h candle
actual_ratio = rows / expected_rows
if actual_ratio < 0.95: FAIL(...)
```

- 95% threshold：容忍 Bitfinex 早期上線初期、維護視窗的零星 missing
- 失敗例：resume-from-DB 邊界 bug 跳過中間整段

對 funding_stats：interval 非固定（事件驅動 / per-funding-tick），改用 max-gap 判斷：
```python
max_gap_ms = max(consecutive_gap_ms)
if max_gap_ms > 1 * 24 * 3600 * 1000:   # 1 day
    FAIL(f"funding_stats {symbol} max_gap = {max_gap_ms / 3600_000}h")
```

### Output 格式

```text
=== Phase 2 Backfill Summary ===
SERIES                       PAGES  ROWS    EARLIEST_MTS         STATUS
candles fUSD 1h p2           42     412345  2020-03-15T...       ✓
candles fUSD 1h p30          ...
funding_stats fUSD           ...
[...]

=== PASS Checks ===
[✓] row_count > 0 for all 8 series
[✓] round-trip exact-match (sampled candles fUSD/p2, funding_stats fUST)
[✓] FRR unit sanity: frr × 86400 / candle.close = 0.87 (in [0.1, 10])
[✗] continuity: candles fUST 1h p30 = 0.91 (rows=98234, expected≈108000)

❌ Phase 2 FAIL — see above
```

### Test coverage 目標

- `funding_stats/{schemas,repository}.py`：100%（純函數 + dataclass）
- `funding_stats/service.py` + `candles/service.py` 新增的 `backfill_*_to_earliest`：所有路徑（empty DB、resume、cursor-stuck、`< limit` 終止、empty-page 終止）都有測試打到
- Mocks：`BitfinexREST` 用 `AsyncMock`；session 用 in-memory sqlite

## Commit Plan（6 commits，獨立可 revert）

```
1. ♻️ Refactor: move FundingStatRow to funding_stats/tables.py
   - new: modules/funding_stats/{__init__.py, tables.py}
   - modify: modules/candles/tables.py (remove FundingStatRow)
   - move: tests/modules/candles/test_tables.py → tests/modules/funding_stats/test_tables.py
   - confirm Atlas migrate diff = no-op
   - all existing tests pass

2. ✨ Feat: funding_stats schemas + repository
   - **first step: curl `/v2/funding/stats/fUSD/hist?limit=1` & 把 array shape 貼進本 spec**（驗證 raw[1]=FRR、raw[2]=AVG_PERIOD、raw[10]=FUNDING_AMOUNT、raw[11]=FUNDING_AMOUNT_USED、raw[15]=FUNDING_BELOW_THRESHOLD）；shape 不符就改本段索引再寫 from_bitfinex
   - new: modules/funding_stats/{schemas,repository}.py
   - new: tests/modules/funding_stats/{test_schemas,test_repository}.py
   - covers: from_bitfinex parse（用驗證後的 shape）, upsert, get_min_mts, get_in_range, get_frr_at_or_before_mts

3. ✨ Feat: BitfinexREST.get_funding_stats
   - modify: external/bitfinex/rest.py (add method; refactor shared HTTP/limiter path if needed)
   - modify: tests/external/bitfinex/test_rest.py (mock 1 funding_stats response — 用 Commit 2 已驗的 shape)

4. ✨ Feat: walking-back service for candles + funding_stats
   - new: modules/backfill/{schemas,errors}.py
     - schemas: SeriesSpec, BackfillStats, BackfillError
     - errors: BackfillCursorStuck（cursor 沒推進時 raise）
   - modify: modules/candles/{repository.py + service.py} (get_min_mts + backfill_candles_to_earliest)
   - new: modules/funding_stats/service.py (backfill_to_earliest)
   - new: tests/modules/{candles,funding_stats}/test_walking_back.py
     - covers: empty DB → start from now; resume from min_mts; cursor-stuck guard;
       <limit termination; empty-page termination

5. ✨ Feat: scripts/backfill_phase2.py orchestrator + 4 PASS checks
   - new: scripts/backfill_phase2.py
   - sequential 8-series loop with per-series session_scope
   - 4 PASS checks (row_count, round-trip spot, FRR sanity, continuity)
   - exit codes: 0 PASS / 1 PASS-check fail / 2 series fail / 3 operational
   - --dry-run flag (schema check only)
   - tests: smoke test only（mock client 跑 1 fake series 確認 wiring 不爆）

6. 📝 Docs: Phase 2 result + FRR unit confirmation
   - new: docs/superpowers/specs/2026-05-10-phase2-result.md
     - 8 series row counts、最早 mts、實際 runtime、PASS check 結果
   - modify: ~/second-brain/wiki/projects/bfx-funding-bot.md
     - Pending → 標 Phase 2 完成
     - Lessons Learned 一行：「FRR 是秒利率（× 86400 = 日利率），實測 ratio = X.XX」
     - Recent Activity 條目
```

## Risks

| Risk | 緩解 |
|---|---|
| Bitfinex `funding/stats` array shape 跟記憶不一致（位置 1/2/10/11/15） | Commit 3 第一步 curl 真 endpoint 對照；不對就改 `from_bitfinex` 索引並更新本 spec |
| Resume-from-DB 在中間有空段時誤判已完成 | Check 4 continuity（≥95% / max-gap）會抓到；失敗 → 明確 fix 而非沉默漏資料 |
| Walking-back 跑超過預期（rows >> 600k） | orchestrator log 每 series 即時印；超 60 min 自己 Ctrl-C，下次 resume 接著走 |
| Bitfinex 429 rate limit 在第 N 個 series 觸發 | 既有 `FundingRateLimiter` 處理；該 series 個別失敗、其他繼續，下次重跑 resume |
| FRR sanity check 失敗（ratio 不在 [0.1, 10]） | 不阻塞 Phase 2 commit 6 — 但 wiki Lessons 標「FRR 單位待解」、Phase 3 swap engine 動作延後 / 需多花一輪研究 |
| `FundingStatRow` 搬模組路徑導致 `Base.metadata` 沒 import | Commit 1 跑 `pytest` + `atlas migrate diff` 雙重驗證 |
| Phase 1 假設 `gap_candles = ceil(gap_minutes / 60)` 是 1h candle | 本 phase 維持單 1h；多 timeframe 標 v3+ |
| funding_stats 資料密度比預期高（千萬筆 / 不是 600k） | walking-back 自動跑完，但 runtime 拉長；orchestrator log 觀察、必要時 sub-window 處理 |

## Time Estimate

~1.5 working day（8-12 hours）。

| Step | 估時 |
|---|---|
| Commit 1（搬 FundingStatRow） | 0.5 h |
| Commit 2（funding_stats schemas + repo + 8-10 tests） | 2 h |
| Commit 3（BitfinexREST.get_funding_stats + curl 驗 shape） | 1 h |
| Commit 4（walking-back service × 2 + tests） | 3 h |
| Commit 5（orchestrator + 4 PASS checks + smoke test） | 2 h |
| 真 backfill 跑一次（20-30 min runtime + log 觀察 + 重試） | 1 h |
| Commit 6（result doc + wiki） | 0.5 h |
| Buffer（Bitfinex shape 不對、continuity 抓到 bug、resume 邊界） | 1-2 h |

## Next Phase

Phase 2 PASS 後 → Phase 3 brainstorm（6 候選策略 backtest matrix：2 symbols × 3 period_agg × 6 strategies = 36 runs；每策略 vs `0.3115%/月 net` baseline 比較；FRR-trend / SpikeDetect / bid-relative-to-FRR 用 Phase 2 已備好的 `funding_stats.repository.get_frr_at_or_before_mts`）。
