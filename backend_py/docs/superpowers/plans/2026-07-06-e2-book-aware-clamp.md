# E2 Book-Aware Rate Clamp + Taker Branch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** submit 時知道自己在 funding book 的哪個位置：不再高掛排隊（overshoot）、不再用 65min 前的 stale close 賤賣 spread（undershoot）、spike 時有保證成交路徑（taker）— 修 2026-07-06 profit review 的第二號 CONFIRMED finding（定價對 live book 零感知）。

**Architecture:** 純 policy 函式（`book_clamp.py`：給定 quote rate + ticker → clamp 後 rate + branch）+ `DeploymentReconciler.deploy()` 內每 symbol 每 tick 一次 public ticker fetch（`GET /v2/ticker/fUST`，免認證，走既有 `FundingRateLimiter`）+ 在建 `DecisionPayload` 時套用 clamp（guard chain、ORDER_SUBMIT event、venue 全部看到 clamp 後的 rate）。**E1 交互**：clamp enabled 時 reprice sweep 的 `ref_rate` 同步對齊 book 競爭價（`max(quote ref, ask − 1 tick)`），否則 sustained spike 中 sweep 會把 E2 剛掛的高價單當 stale 自砍（cancel/repost churn）。預設 observe-only（`BFX_CLAMP_ENABLED=false`：抓 ticker、log `clamp_would_adjust`、submit 行為與現狀 byte-identical——含 sweep ref 不變）。

**Tech Stack:** Python 3.13 + uv、pytest（asyncio auto mode，直接寫 `async def test_*`）、mypy strict、ruff。

**背景（實作者必讀，5 分鐘）：** `backend_py/docs/research/2026-07-06-profit-design-review.md` §0 + §1 E2 段。架構圖：`backend_py/ARCHITECTURE.md` §3、§4（步驟 7/7b）。E1 已 merge（`cf29d3b`..`e50427f`）：`deployment/reprice.py` + `reconciler._reprice_sweep` 是本 plan 的鄰居與依賴。

## Global Constraints

- 所有指令在 `backend_py/` 下跑：`cd backend_py && uv run ...`（repo root 會撞 pyenv 3.12）。
- **I-SW single-writer 不變**：clamp 只調 submit rate；POST/SKIP 決策、period、amount（sizing）權威全部不動。clamp 不回寫 StandingQuote、不碰 ledger/tracker。
- **Fail-safe**：ticker fetch 失敗（任何 exception）→ log warning + `ticker=None` → 本 tick 所有 fill 走 fallback（= 現狀行為）；**ticker 失敗絕不得擋住 gap 部署**。public endpoint 免認證，無 `ExecutorAuthError` 疑慮。
- **Anti-undersell policy（不可放寬）**：
  - taker 分支需同時滿足 `bid ≥ quote.rate`（fill rate 繼承 bid ≥ signal floor）**且** `bid_size ≥ amount`（防 partial fill 後殘量以低價 rest）**且** `bid_period ≤ taker_max_period_days`（防繼承超長天期鎖倉）。
  - down-clamp（undercut）以 `max_down_pct` 為界：競爭價低於 `quote.rate × (1 − max_down_pct)` → 放棄 clamp、掛原 quote.rate（>15% 的下移是 regime 判斷，屬 signal 層職權，下一個 1h boundary 由 MR gate 裁決）。
  - up-clamp（raise）無上界 — 風險不對稱：掛太高不成交的最壞情況由 E1 sweep 在 min_age 後收斂（閒置有界 ~30-60min）；以爛 rate 成交鎖 2 天則不可逆。
- **Observe mode = 零行為差**：`BFX_CLAMP_ENABLED=false` 時 submit rate 與 sweep ref 都維持現狀，只多 ticker fetch + log。
- `TICK = 1e-8`（Bitfinex funding rate 最小跳動，Go-era `pricing.go` `minTickSize` 常數）— 是 venue 屬性不是 policy，寫死常數、不做 env。
- ticker 的 BID/ASK/FRR 是**日利率 decimal**（0.0002 = 0.02%/day），與 `quote.rate`（candle close）同尺度，直接比較；`funding_stats.frr`（~1e-6）是不同量，**不可**當價格來源（歷史踩坑見 `live_attribution.py:57-82`）。
- 每個 commit 前：`uv run pytest -m "not integration"` 全綠 + `uv run mypy src/` + `uv run ruff check` 無誤。
- Commit 訊息用 repo emoji prefix（`✨ Feat:` / `✅ Test:` / `📝 Docs:`）。
- 新 env vars（全帶安全預設，未設定 = 現狀行為 + observe log）：`BFX_CLAMP_ENABLED`（default false）、`BFX_CLAMP_MAX_DOWN_PCT`（default 0.15）、`BFX_CLAMP_TAKER_MAX_PERIOD_D`（default 7）。

---

### Task 1: FundingTicker + `BitfinexREST.get_funding_ticker`

**Files:**
- Modify: `src/bfx_funding_bot/external/bitfinex/rest.py`（加 `FundingTicker` dataclass + `get_funding_ticker` method）
- Test: `tests/external/bitfinex/test_rest_ticker.py`（新檔，self-contained）

**Interfaces:**
- Consumes: 既有 `BitfinexREST`（`rest.py:30`，constructor `http/base_url/limiter`）、`BitfinexAPIError`/`BitfinexRateLimited`/`BitfinexShapeError`（`external/bitfinex/errors.py`）、`FundingRateLimiter`（`external/bitfinex/rate_limit.py`）
- Produces: `FundingTicker`（frozen slots dataclass：`symbol: str, frr: float, bid: float, bid_period: int, bid_size: float, ask: float, ask_period: int, ask_size: float`）+ `async def get_funding_ticker(self, *, symbol: str) -> FundingTicker` — Task 2/3 依賴這些名稱與簽名。

- [ ] **Step 1: Write the failing tests**

```python
# tests/external/bitfinex/test_rest_ticker.py
"""GET /v2/ticker/f{symbol} — E2 book-aware clamp 的唯一 book 資料源。

REST funding ticker 為 17 欄 flat array（WS 版 16 欄 + 尾端 FIRST_TRADE）：
[FRR, BID, BID_PERIOD, BID_SIZE, ASK, ASK_PERIOD, ASK_SIZE, DAILY_CHANGE,
 DAILY_CHANGE_PERC, LAST_PRICE, VOLUME, HIGH, LOW, _PLH, _PLH,
 FRR_AMOUNT_AVAILABLE, FIRST_TRADE]
clamp 只需前 7 欄。
"""
import httpx
import pytest

from bfx_funding_bot.external.bitfinex.errors import (
    BitfinexAPIError,
    BitfinexRateLimited,
    BitfinexShapeError,
)
from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter
from bfx_funding_bot.external.bitfinex.rest import BitfinexREST, FundingTicker

_ROW = [
    0.0002, 0.00018, 2, 50_000.0, 0.00021, 30, 120_000.0,
    0.00001, 0.05, 0.0002, 1_000_000.0, 0.00025, 0.00015,
    None, None, 250_000.0, 0.00019,
]


def _client(handler) -> tuple[httpx.AsyncClient, BitfinexREST]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rest = BitfinexREST(
        http=http, base_url="https://api-pub.bitfinex.com",
        limiter=FundingRateLimiter(),
    )
    return http, rest


async def test_get_funding_ticker_parses_funding_array():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_ROW)

    http, rest = _client(handler)
    async with http:
        t = await rest.get_funding_ticker(symbol="fUST")
    assert captured["url"] == "https://api-pub.bitfinex.com/v2/ticker/fUST"
    assert t == FundingTicker(
        symbol="fUST", frr=0.0002, bid=0.00018, bid_period=2, bid_size=50_000.0,
        ask=0.00021, ask_period=30, ask_size=120_000.0,
    )


async def test_get_funding_ticker_tolerates_missing_f_prefix():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_ROW)

    http, rest = _client(handler)
    async with http:
        t = await rest.get_funding_ticker(symbol="UST")
    assert captured["url"].endswith("/v2/ticker/fUST")
    assert t.symbol == "fUST"


async def test_get_funding_ticker_raises_on_http_error():
    http, rest = _client(lambda r: httpx.Response(500, text="boom"))
    async with http:
        with pytest.raises(BitfinexAPIError):
            await rest.get_funding_ticker(symbol="fUST")


async def test_get_funding_ticker_raises_on_429():
    http, rest = _client(lambda r: httpx.Response(429, headers={"Retry-After": "5"}))
    async with http:
        with pytest.raises(BitfinexRateLimited):
            await rest.get_funding_ticker(symbol="fUST")


async def test_get_funding_ticker_rejects_non_list():
    http, rest = _client(lambda r: httpx.Response(200, json={"oops": 1}))
    async with http:
        with pytest.raises(BitfinexShapeError):
            await rest.get_funding_ticker(symbol="fUST")


async def test_get_funding_ticker_rejects_short_row():
    http, rest = _client(lambda r: httpx.Response(200, json=[0.0002, 0.00018]))
    async with http:
        with pytest.raises(BitfinexShapeError):
            await rest.get_funding_ticker(symbol="fUST")


async def test_get_funding_ticker_rejects_null_book_field():
    row = list(_ROW)
    row[1] = None  # BID null（空 book 邊）→ fail-closed，讓呼叫方 fallback
    http, rest = _client(lambda r: httpx.Response(200, json=row))
    async with http:
        with pytest.raises(BitfinexShapeError):
            await rest.get_funding_ticker(symbol="fUST")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_rest_ticker.py -v`
Expected: FAIL — `ImportError: cannot import name 'FundingTicker' from 'bfx_funding_bot.external.bitfinex.rest'`

- [ ] **Step 3: Write the implementation**

`rest.py` 修改。(a) 兩個新 import 併入 stdlib import 區的 isort 位置（`import logging` 之後、空行與 `import httpx` 之前；repo ruff 啟用 `I` 規則，排錯位置 `uv run ruff check` 會報 I001 — `--fix` 可自動整理）：

```python
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx
```

(b) `logger = logging.getLogger(__name__)` 之後、`_bitfinex_period_agg_path` 之前加：

```python
@dataclass(frozen=True, slots=True)
class FundingTicker:
    """GET /v2/ticker/{fSymbol} — funding book 快照（E2 book-aware clamp）。

    Rate 欄位是日利率 decimal（0.0002 = 0.02%/day），與 funding_candles.close
    同尺度，可直接與 StandingQuote.rate 比較。funding_stats 的 frr（~1e-6）
    是不同量，勿混用（see live_attribution.assert_market_rate_band）。
    """
    symbol: str
    frr: float          # Flash Return Rate（過去 1h 平均固定利率）
    bid: float          # best bid rate（借方最高出價；我方吃單即成交）
    bid_period: int     # bid 天期 — taker fill 會繼承這個 period
    bid_size: float
    ask: float          # best ask rate（貸方最低要價 = 隊首）
    ask_period: int
    ask_size: float

    @classmethod
    def from_bitfinex(cls, entry: list[Any], *, symbol: str) -> "FundingTicker":
        # REST 回 17 欄（WS 16 欄 + FIRST_TRADE）；clamp 只需前 7 欄。
        if len(entry) < 7:
            raise ValueError(f"funding ticker too short: {len(entry)} fields")
        head = entry[:7]
        if any(v is None for v in head):
            # 空 book 邊 → fail-closed：讓呼叫方走 fallback（= 無 clamp）
            raise ValueError(f"null field in funding ticker head: {head!r}")
        return cls(
            symbol=symbol,
            frr=float(entry[0]),
            bid=float(entry[1]),
            bid_period=int(entry[2]),
            bid_size=float(entry[3]),
            ask=float(entry[4]),
            ask_period=int(entry[5]),
            ask_size=float(entry[6]),
        )
```

(c) `get_funding_stats` 之後加 method（錯誤處理逐行對齊既有兩個 method 的慣例）：

```python
    async def get_funding_ticker(self, *, symbol: str) -> FundingTicker:
        """Pull the live funding ticker (best bid/ask) for one symbol.

        Endpoint: GET /v2/ticker/f{symbol}（免認證；funding schema 17 欄）

        E2 book-aware clamp 的唯一 book 資料源（docs/research/
        2026-07-06-profit-design-review.md §1 E2）。每 ~90s reconcile tick
        每 symbol 一 call，走共用 FundingRateLimiter（30/min budget）。

        Args:
            symbol: e.g. "fUST", "fUSD". Tolerates leading "f".
        """
        sym = symbol[1:] if symbol.startswith("f") else symbol
        path = f"/v2/ticker/f{sym}"

        async with self._limiter.acquire():
            try:
                resp = await self._http.get(f"{self._base_url}{path}", timeout=30.0)
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
                f"expected funding ticker list, got {type(payload).__name__}: {payload!r}"
            )

        try:
            return FundingTicker.from_bitfinex(payload, symbol=f"f{sym}")
        except (ValueError, TypeError) as e:
            raise BitfinexShapeError(
                f"funding ticker parse failed: {e}; raw={payload!r}"
            ) from e
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_rest_ticker.py -v`
Expected: 7 PASS

- [ ] **Step 5: Quality gates + commit**

```bash
cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check
git add src/bfx_funding_bot/external/bitfinex/rest.py tests/external/bitfinex/test_rest_ticker.py
git commit -m "✨ Feat: E2 FundingTicker + BitfinexREST.get_funding_ticker（public book 快照）"
```

---

### Task 2: Book-clamp 純 policy 函式

**Files:**
- Create: `src/bfx_funding_bot/modules/execution/deployment/book_clamp.py`
- Test: `tests/modules/execution/deployment/test_book_clamp.py`

**Interfaces:**
- Consumes: Task 1 的 `FundingTicker`
- Produces: `TICK: float`（= 1e-8）、`ClampBranch`（StrEnum：`TAKER/UNDERCUT/RAISE/FLOOR/FALLBACK`）、`ClampDecision`（frozen dataclass：`rate: float, branch: ClampBranch`）、`ClampPolicy`（frozen dataclass：`enabled: bool, max_down_pct: float, taker_max_period_days: int`）、`clamp_rate(*, quote_rate, amount, ticker, policy) -> ClampDecision`、`clamp_policy_from_env(environ) -> ClampPolicy` — Task 3/4 依賴這些名稱與簽名。

- [ ] **Step 1: Write the failing tests**

```python
# tests/modules/execution/deployment/test_book_clamp.py
from bfx_funding_bot.external.bitfinex.rest import FundingTicker
from bfx_funding_bot.modules.execution.deployment.book_clamp import (
    TICK,
    ClampBranch,
    ClampPolicy,
    clamp_policy_from_env,
    clamp_rate,
)

_POLICY = ClampPolicy(enabled=True, max_down_pct=0.15, taker_max_period_days=7)


def _ticker(
    *, bid: float = 0.00018, bid_period: int = 2, bid_size: float = 1_000.0,
    ask: float = 0.00021,
) -> FundingTicker:
    return FundingTicker(
        symbol="fUST", frr=0.0002, bid=bid, bid_period=bid_period,
        bid_size=bid_size, ask=ask, ask_period=2, ask_size=5_000.0,
    )


def test_no_ticker_falls_back():
    got = clamp_rate(quote_rate=0.0002, amount=200.0, ticker=None, policy=_POLICY)
    assert (got.rate, got.branch) == (0.0002, ClampBranch.FALLBACK)


def test_degenerate_book_falls_back():
    for t in (_ticker(bid=0.0), _ticker(ask=TICK)):
        got = clamp_rate(quote_rate=0.0002, amount=200.0, ticker=t, policy=_POLICY)
        assert got.branch is ClampBranch.FALLBACK
        assert got.rate == 0.0002


def test_taker_when_bid_at_or_above_quote():
    # bid 0.00018 ≥ quote 0.00015 → 吃單保證成交，rate 維持 quote（fill 繼承
    # bid 的 rate/period，實得 ≥ quote = signal floor 不破）
    got = clamp_rate(quote_rate=0.00015, amount=200.0, ticker=_ticker(), policy=_POLICY)
    assert (got.rate, got.branch) == (0.00015, ClampBranch.TAKER)


def test_taker_requires_full_bid_size():
    # bid_size 100 < amount 200 → partial fill 殘量會以低價 rest → 不吃，走 maker
    got = clamp_rate(
        quote_rate=0.00015, amount=200.0, ticker=_ticker(bid_size=100.0), policy=_POLICY,
    )
    assert got.branch is ClampBranch.RAISE


def test_taker_requires_short_bid_period():
    # bid_period 30 > 7 → 吃單會鎖 30 天倉 → 不吃，走 maker
    got = clamp_rate(
        quote_rate=0.00015, amount=200.0, ticker=_ticker(bid_period=30), policy=_POLICY,
    )
    assert got.branch is ClampBranch.RAISE


def test_raise_lifts_stale_quote_to_book_front():
    # bid 0.00018 < quote 0.00019 < ask−tick → 掛 ask−tick 搶隊首，不賤賣 spread
    got = clamp_rate(quote_rate=0.00019, amount=200.0, ticker=_ticker(), policy=_POLICY)
    assert (got.rate, got.branch) == (0.00020999, ClampBranch.RAISE)


def test_undercut_when_queued_behind():
    # quote 0.00023 > ask−tick 0.00020999，且下移 < 15% → 降到隊首防排隊
    got = clamp_rate(quote_rate=0.00023, amount=200.0, ticker=_ticker(), policy=_POLICY)
    assert (got.rate, got.branch) == (0.00020999, ClampBranch.UNDERCUT)


def test_floor_stops_deep_down_clamp():
    # 競爭價 0.00020999 < quote 0.0005 × 0.85 → 不追砍，維持 quote（regime
    # 下移交給下一個 1h boundary 的 signal 層裁決）
    got = clamp_rate(quote_rate=0.0005, amount=200.0, ticker=_ticker(), policy=_POLICY)
    assert (got.rate, got.branch) == (0.0005, ClampBranch.FLOOR)


def test_floor_boundary_exact_is_undercut():
    # 邊界：競爭價恰等於 quote×(1−max_down) → 不觸 floor（嚴格小於才觸）。
    # max_down=0.5、quote=0.0004 → 界 = 0.0002（×0.5 是 exact halving，float 安全）；
    # ask = 0.0002 + TICK → 競爭價 round 後恰為 0.0002。
    policy = ClampPolicy(enabled=True, max_down_pct=0.5, taker_max_period_days=7)
    got = clamp_rate(
        quote_rate=0.0004, amount=200.0,
        ticker=_ticker(bid=0.0001, ask=0.00020001), policy=policy,
    )
    assert (got.rate, got.branch) == (0.0002, ClampBranch.UNDERCUT)


def test_competitive_rate_rounds_float_dust():
    # 0.0002 − 1e-8 的浮點尾差要被 round 掉，輸出正好在 1e-8 grid 上。
    # quote 取 0.00021：下移 <15% 不觸 FLOOR（0.00021×0.85=0.0001785 < 0.00019999）
    # → 走 UNDERCUT。（勿用更高的 quote — 會觸 FLOOR 回原價，測不到 rounding。）
    got = clamp_rate(
        quote_rate=0.00021, amount=200.0,
        ticker=_ticker(bid=0.0001, ask=0.0002), policy=_POLICY,
    )
    assert (got.rate, got.branch) == (0.00019999, ClampBranch.UNDERCUT)


def test_clamp_policy_from_env_defaults():
    p = clamp_policy_from_env({})
    assert p == ClampPolicy(enabled=False, max_down_pct=0.15, taker_max_period_days=7)


def test_clamp_policy_from_env_enabled_variants():
    for truthy in ("1", "true", "TRUE", "yes"):
        assert clamp_policy_from_env({"BFX_CLAMP_ENABLED": truthy}).enabled is True
    assert clamp_policy_from_env({"BFX_CLAMP_ENABLED": "false"}).enabled is False


def test_clamp_policy_from_env_overrides():
    p = clamp_policy_from_env({
        "BFX_CLAMP_MAX_DOWN_PCT": "0.30",
        "BFX_CLAMP_TAKER_MAX_PERIOD_D": "2",
    })
    assert p.max_down_pct == 0.30
    assert p.taker_max_period_days == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_book_clamp.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bfx_funding_bot.modules.execution.deployment.book_clamp'`

- [ ] **Step 3: Write the implementation**

```python
# src/bfx_funding_bot/modules/execution/deployment/book_clamp.py
"""Book-aware rate clamp (E2 — docs/research/2026-07-06-profit-design-review.md §1).

純 policy：submit 前把 signal 層的 quote rate 對齊 live funding book。
執行（ticker fetch、observe/enforce）由 DeploymentReconciler 負責；本模組零 I/O。

分支（優先序）：
  1. TAKER    — bid ≥ quote.rate 且 bid_size ≥ amount 且 bid_period ≤ 上限
                → 掛 quote.rate 直接吃單（唯一保證成交路徑；fill 繼承 bid 的
                rate/period，rate ≥ quote.rate = signal floor 不破）。
  2. FALLBACK — 無 ticker / book 退化（bid≤0 或 ask≤TICK）→ quote.rate 原樣
                （= 現狀行為，fail-closed）。
  3. FLOOR    — 競爭價（ask − TICK）低於 quote.rate×(1−max_down_pct)
                → 放棄 clamp、掛原價排隊。>max_down 的下移是 regime 判斷，
                屬 signal 層職權（下一個 1h boundary 由 MR gate 裁決）。
  4. UNDERCUT — quote 高於競爭價（排隊尾）→ 降到 ask − TICK 搶隊首。
     RAISE    — quote 低於競爭價（賤賣 spread）→ 抬到 ask − TICK。

風險不對稱（為什麼 down 有 floor、up 沒有）：clamp-up 最壞 = 掛太高不成交，
E1 reprice sweep 在 min_age 後收斂（閒置有界 ~30-60min）；clamp-down 最壞 =
以爛 rate 成交鎖 2 天（不可逆）。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from bfx_funding_bot.external.bitfinex.rest import FundingTicker

# Bitfinex funding rate 最小跳動（Go-era pricing.go minTickSize；venue 屬性，
# 非 policy）。比 best ask 低 1 tick = price priority 隊首。
TICK: float = 1e-8


class ClampBranch(StrEnum):
    TAKER = "taker"
    UNDERCUT = "undercut"
    RAISE = "raise"
    FLOOR = "floor"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class ClampPolicy:
    enabled: bool                # False = observe-only（log would_adjust，掛原 rate）
    max_down_pct: float          # 0.15 = 競爭價低於 quote 15% 以上 → 不追，掛原價
    taker_max_period_days: int   # taker fill 繼承 bid_period；超過此天期不吃單


def clamp_policy_from_env(environ: Mapping[str, str]) -> ClampPolicy:
    """從 env 建 policy；全部未設定 = observe-only 安全預設。"""
    return ClampPolicy(
        enabled=environ.get("BFX_CLAMP_ENABLED", "false").lower() in ("1", "true", "yes"),
        max_down_pct=float(environ.get("BFX_CLAMP_MAX_DOWN_PCT", "0.15")),
        taker_max_period_days=int(environ.get("BFX_CLAMP_TAKER_MAX_PERIOD_D", "7")),
    )


@dataclass(frozen=True, slots=True)
class ClampDecision:
    rate: float
    branch: ClampBranch


def clamp_rate(
    *,
    quote_rate: float,
    amount: float,
    ticker: FundingTicker | None,
    policy: ClampPolicy,
) -> ClampDecision:
    """單筆 fill 的 clamp 決策。純函式；對 policy.enabled 無感 —
    observe/enforce 由呼叫方（reconciler）決定。"""
    if ticker is None or ticker.bid <= 0.0 or ticker.ask <= TICK:
        return ClampDecision(rate=quote_rate, branch=ClampBranch.FALLBACK)
    if (
        ticker.bid >= quote_rate
        and ticker.bid_size >= amount
        and ticker.bid_period <= policy.taker_max_period_days
    ):
        return ClampDecision(rate=quote_rate, branch=ClampBranch.TAKER)
    # round 消 float 減法尾差；rate 值 ~1e-4、grid 1e-8 → 10 位小數無損
    competitive = round(ticker.ask - TICK, 10)
    if competitive < quote_rate * (1.0 - policy.max_down_pct):
        return ClampDecision(rate=quote_rate, branch=ClampBranch.FLOOR)
    if competitive >= quote_rate:
        return ClampDecision(rate=competitive, branch=ClampBranch.RAISE)
    return ClampDecision(rate=competitive, branch=ClampBranch.UNDERCUT)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_book_clamp.py -v`
Expected: 13 PASS

- [ ] **Step 5: Quality gates + commit**

```bash
cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check
git add src/bfx_funding_bot/modules/execution/deployment/book_clamp.py tests/modules/execution/deployment/test_book_clamp.py
git commit -m "✨ Feat: E2 book-clamp 純 policy（taker/undercut/raise + max-down floor）"
```

---

### Task 3: DeploymentReconciler 整合（ticker fetch + clamp 套用 + sweep ref 對齊）

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/deployment/reconciler.py`（constructor + `deploy()` ticker fetch + fills 迴圈 clamp + `_reprice_sweep` ref 對齊）
- Test: `tests/modules/execution/deployment/test_reconciler.py`（新增 clamp 測試 + `_build` 擴充）

**Interfaces:**
- Consumes: Task 1 `FundingTicker`/`get_funding_ticker` 簽名、Task 2 全部、既有 `_reprice_sweep`（`reconciler.py:309`）與 fills 迴圈（`reconciler.py:242-280`）
- Produces: `DeploymentReconciler(..., ticker_source: _TickerSourceProtocol | None = None, clamp: ClampPolicy | None = None)`；`_TickerSourceProtocol`（private Protocol：`async def get_funding_ticker(self, *, symbol: str) -> FundingTicker`）；`_reprice_sweep(..., book_competitive: float | None = None)` — Task 4 daemon wiring 依賴 constructor 參數。`ticker_source=None` 或 `clamp=None` 時行為與現狀 byte-identical。

- [ ] **Step 1: Write the failing tests**

在 `tests/modules/execution/deployment/test_reconciler.py` 檔尾加（檔頭 import 補兩行，按 isort 字母序插入：`from bfx_funding_bot.external.bitfinex.rest import FundingTicker` 緊接既有 `...external.bitfinex.auth_rest` import 之後；`from bfx_funding_bot.modules.execution.deployment.book_clamp import ClampPolicy` 放在 `...deployment.reconciler import DeploymentReconciler` 之前。既有 `_FakeCanceller`/`_venue_offer`/`_REPRICE`/`_post_quote`/`_build` 沿用，`_build` 擴充見下）：

```python
_CLAMP_ON = ClampPolicy(enabled=True, max_down_pct=0.15, taker_max_period_days=7)
_CLAMP_OBSERVE = ClampPolicy(enabled=False, max_down_pct=0.15, taker_max_period_days=7)


def _fticker(
    *, bid: float = 0.00005, bid_period: int = 2, bid_size: float = 100_000.0,
    ask: float = 0.001,
) -> FundingTicker:
    return FundingTicker(
        symbol="fUST", frr=0.0002, bid=bid, bid_period=bid_period,
        bid_size=bid_size, ask=ask, ask_period=2, ask_size=100_000.0,
    )


class _FakeTickerSource:
    def __init__(self, ticker: FundingTicker) -> None:
        self.ticker = ticker
        self.fetched: list[str] = []

    async def get_funding_ticker(self, *, symbol: str) -> FundingTicker:
        self.fetched.append(symbol)
        return self.ticker


class _BoomTickerSource:
    async def get_funding_ticker(self, *, symbol: str) -> FundingTicker:
        raise RuntimeError("venue 500")


async def test_clamp_raises_stale_quote_to_book_front():
    # quote 0.00012（_post_quote），book ask 0.001（spike）→ 掛 ask−tick
    ts = _FakeTickerSource(_fticker())
    rec, ex, _, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")],
        ticker_source=ts, clamp=_CLAMP_ON,
    )
    await rec.deploy()
    assert ts.fetched == ["fUST"]
    assert len(ex.submitted) == 1
    assert ex.submitted[0].offer_rate == 0.00099999  # round(0.001 - 1e-8, 10)


async def test_clamp_observe_mode_submits_quote_rate():
    ts = _FakeTickerSource(_fticker())
    rec, ex, _, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")],
        ticker_source=ts, clamp=_CLAMP_OBSERVE,
    )
    await rec.deploy()
    assert ts.fetched == ["fUST"]  # observe mode 照抓 ticker（rollout 需要 log）
    assert ex.submitted[0].offer_rate == 0.00012  # 但 submit 行為 = 現狀


async def test_clamp_taker_keeps_quote_rate():
    # bid 0.0002 ≥ quote 0.00012，size/period 都在界內 → taker（rate 不變）
    ts = _FakeTickerSource(_fticker(bid=0.0002, ask=0.00021))
    rec, ex, _, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")],
        ticker_source=ts, clamp=_CLAMP_ON,
    )
    await rec.deploy()
    assert ex.submitted[0].offer_rate == 0.00012


async def test_clamp_floor_keeps_quote_rate():
    # book 崩到 ask 0.00005 → 競爭價 < 0.00012×0.85 → 不追砍，掛原價
    ts = _FakeTickerSource(_fticker(bid=0.00001, ask=0.00005))
    rec, ex, _, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")],
        ticker_source=ts, clamp=_CLAMP_ON,
    )
    await rec.deploy()
    assert ex.submitted[0].offer_rate == 0.00012


async def test_clamp_ticker_fetch_error_falls_back_and_deploys():
    rec, ex, _, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")],
        ticker_source=_BoomTickerSource(), clamp=_CLAMP_ON,
    )
    await rec.deploy()  # fetch 失敗絕不擋部署
    assert len(ex.submitted) == 1
    assert ex.submitted[0].offer_rate == 0.00012


async def test_no_clamp_config_never_fetches():
    ts = _FakeTickerSource(_fticker())
    rec, ex, _, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")],
        ticker_source=ts,  # clamp=None（預設）
    )
    await rec.deploy()
    assert ts.fetched == []
    assert ex.submitted[0].offer_rate == 0.00012


async def test_sweep_ref_aligns_to_book_when_clamp_enabled():
    # E1×E2 交互：sustained spike 中（ask 0.001 維持高檔），E2 上一 tick 以
    # ~ask−tick 掛出的單（0.00099，齡 60min）不可被 sweep 當 stale 自砍 —
    # ref 對齊 max(quote 0.00012, ask−tick 0.00099999) → 0.00099 在容忍內。
    canc = _FakeCanceller()
    ts = _FakeTickerSource(_fticker())
    rec, _, _, _ = _build(
        exposure=D("570"), quotes=[_post_quote("fUST_a30")],
        canceller=canc, reprice=_REPRICE,
        ticker_source=ts, clamp=_CLAMP_ON,
    )
    await rec.deploy(venue_offers=(_venue_offer("42", 0.00099),))
    assert canc.cancelled == []


async def test_sweep_ref_unchanged_in_observe_mode():
    # observe mode = 零行為差：sweep ref 仍用 quote ref（0.00012×1.1），
    # 0.00099 是 stale → 照砍（與 E1 現狀 byte-identical）
    canc = _FakeCanceller()
    ts = _FakeTickerSource(_fticker())
    rec, _, _, _ = _build(
        exposure=D("570"), quotes=[_post_quote("fUST_a30")],
        canceller=canc, reprice=_REPRICE,
        ticker_source=ts, clamp=_CLAMP_OBSERVE,
    )
    await rec.deploy(venue_offers=(_venue_offer("42", 0.00099),))
    assert canc.cancelled == ["42"]
```

`_build` 擴充（`test_reconciler.py:145` 起的既有 helper，加兩個參數傳給 constructor；其餘不動）：

```python
def _build(*, exposure, quotes, safety_allowed=True, executor=None, safety=None,
           available=None, event_sink=None, canceller=None, reprice=None,
           ticker_source=None, clamp=None):
    ...
    rec = DeploymentReconciler(
        ...,  # 既有參數全部照舊
        canceller=canceller,
        reprice=reprice,
        ticker_source=ticker_source,
        clamp=clamp,
    )
    return rec, ex, tracker, safety
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_reconciler.py -v -k clamp`
Expected: FAIL — `TypeError: DeploymentReconciler.__init__() got an unexpected keyword argument 'ticker_source'`

- [ ] **Step 3: Implement**

`reconciler.py` 修改（依既有 style）：

(a) imports 補（按 isort 字母序插入：`rest` import 緊接 `reconciler.py:16` 的 `auth_rest` import 之後；`book_clamp` import 放在 `...deployment.reprice` import 之前 — 排錯位置 ruff I001 會擋 commit，`--fix` 可自動整理）：

```python
from bfx_funding_bot.external.bitfinex.rest import FundingTicker
from bfx_funding_bot.modules.execution.deployment.book_clamp import (
    TICK,
    ClampBranch,
    ClampPolicy,
    clamp_rate,
)
```

(b) `_EventSinkProtocol` 之後加 private protocol（同 `_LedgerProtocol` 慣例）：

```python
class _TickerSourceProtocol(Protocol):
    async def get_funding_ticker(self, *, symbol: str) -> FundingTicker: ...
```

(c) constructor 參數表尾端（`reprice: RepricePolicy | None = None,` 之後）加：

```python
        ticker_source: _TickerSourceProtocol | None = None,
        clamp: ClampPolicy | None = None,
```

`__init__` 體加：

```python
        self._ticker_source = ticker_source
        self._clamp = clamp
```

(d) `deploy()` symbol 迴圈內，`symbol_cells = [...]` 之後、`e_total = ...` 之前插入：

```python
            # E2 book-aware clamp：每 symbol 每 tick 一次 public ticker（免認證，
            # 走共用 FundingRateLimiter，~2 call/90s ≪ 30/min budget）。抓不到
            # → ticker=None → 本 tick 全 fallback（= 現狀行為）；絕不擋 deploy。
            ticker: FundingTicker | None = None
            if self._clamp is not None and self._ticker_source is not None:
                try:
                    ticker = await self._ticker_source.get_funding_ticker(symbol=symbol)
                except Exception:
                    log.warning("clamp_ticker_fetch_failed symbol=%s", symbol, exc_info=True)
            # clamp enabled 時 sweep ref 對齊 book 競爭價（新掛單就掛在這個價位）：
            # 否則 sustained spike 中 sweep 會把 E2 剛掛的高價單當 stale 自砍
            # （cancel/repost churn）。只會抬高 ref（更少 cancel、更保守）；
            # observe mode 不對齊（零行為差）。
            book_competitive = (
                round(ticker.ask - TICK, 10)
                if ticker is not None
                and self._clamp is not None and self._clamp.enabled
                and ticker.ask > TICK
                else None
            )
```

(e) E1 sweep 呼叫處（`if self._reprice is not None and venue_offers:` 區塊）補傳參數：

```python
            if self._reprice is not None and venue_offers:
                cancel_budget -= await self._reprice_sweep(
                    symbol=symbol,
                    symbol_cells=symbol_cells,
                    venue_offers=venue_offers,
                    now=now,
                    budget=cancel_budget,
                    book_competitive=book_competitive,
                )
```

(f) fills 迴圈（`for cell_id, amount in fills.items():`）內，`quote = ...` / `if quote is None: continue` 之後、`decision = DecisionPayload(...)` 之前插入，並把 `offer_rate=quote.rate` 改為 `offer_rate=offer_rate`：

```python
                # E2 clamp：guard chain、ORDER_SUBMIT event、venue 全部看到
                # clamp 後的 rate（單一 rate 真相）。observe mode 只 log。
                offer_rate = quote.rate
                if self._clamp is not None and ticker is not None and quote.rate is not None:
                    cd = clamp_rate(
                        quote_rate=quote.rate, amount=float(amount),
                        ticker=ticker, policy=self._clamp,
                    )
                    if cd.rate != quote.rate or cd.branch is ClampBranch.TAKER:
                        log.info(
                            "clamp_%s cell=%s branch=%s quote_rate=%s clamped=%s "
                            "bid=%s ask=%s bid_period=%s amount=%s",
                            "applied" if self._clamp.enabled else "would_adjust",
                            cell_id, cd.branch, quote.rate, cd.rate,
                            ticker.bid, ticker.ask, ticker.bid_period, amount,
                        )
                    if self._clamp.enabled:
                        offer_rate = cd.rate
                decision = DecisionPayload(
                    decision_outcome=DecisionOutcome.POST,
                    signal_correlation_id=quote.signal_correlation_id,
                    offer_rate=offer_rate,
                    offer_amount_usdt=float(amount),
                    offer_duration_days=quote.period_days,
                    symbol=self._cell_symbol[cell_id],
                )
```

(g) `_reprice_sweep` 簽名加參數 + ref 對齊（`assert ref.rate is not None` 之後）：

```python
    async def _reprice_sweep(
        self,
        *,
        symbol: str,
        symbol_cells: list[CellConfig],
        venue_offers: tuple[ActiveFundingOffer, ...],
        now: int,
        budget: int,
        book_competitive: float | None = None,
    ) -> int:
```

```python
        ref = max(quotes, key=lambda q: q.rate or 0.0)
        assert ref.rate is not None  # POST quote 的 rate 必非 None
        ref_rate = ref.rate
        if book_competitive is not None:
            # E2：ref 對齊「現在會掛出的價」（clamp 後）。max() 只會抬高 ref
            # （更少 cancel）；book 下移不加速砍單 — reprice-down 的節奏仍由
            # quote 每小時更新決定（E1 語意不變）。
            ref_rate = max(ref_rate, book_competitive)
        candidates = stale_offers(
            offers=[o for o in venue_offers if o.symbol == symbol],
            ref_rate=ref_rate,
            now_ms=now,
            policy=self._reprice,
        )
```

同函式內兩處 log 的 `ref.rate` 改為 `ref_rate`（`reprice_would_cancel` 與 `reprice_cancelled` 的 `ref_rate=%s` 參數）；cancel 的 `signal_correlation_id=ref.signal_correlation_id` 不變。

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/ -v`
Expected: 全 PASS（既有 E1/deploy 測試不帶 ticker_source/clamp → 行為不變；新增 8 個 clamp 測試綠）

- [ ] **Step 5: Quality gates + commit**

```bash
cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check
git add -u
git commit -m "✨ Feat: E2 DeploymentReconciler book-aware clamp（observe 預設）+ sweep ref 對齊防自砍"
```

---

### Task 4: Daemon wiring + env + 文件

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py`（DeploymentReconciler 建構處 :1029-1060 附近，`if not spec.is_simulated:` 區塊內）
- Modify: `src/bfx_funding_bot/modules/execution/deployment/standing_quote.py:1-7`（module docstring）
- Modify: `ARCHITECTURE.md` §4（步驟 7c + 參數總覽）與 §9（invariant I-BC）

**Interfaces:**
- Consumes: Task 2 `clamp_policy_from_env`、Task 3 constructor 參數；daemon 既有 public REST instance `bitfinex`（`daemon.py:691-695` 建構，`build_daemon()` 同 scope 可直接注入）
- Produces: 部署後 daemon 帶 clamp（預設 observe-only）

- [ ] **Step 1: Wire daemon**

`daemon.py` 檔頭 import 區補，按 isort 字母序插在 `from bfx_funding_bot.modules.execution.deployment.reconciler import DeploymentReconciler`（daemon.py:61）**之前**（`book_clamp` < `reconciler` < `reprice`；放錯位置 ruff I001 會擋 gate）：

```python
from bfx_funding_bot.modules.execution.deployment.book_clamp import clamp_policy_from_env
```

`DeploymentReconciler(...)` 建構（`reprice=policy_from_env(os.environ),` 之後）加：

```python
            # E2 book-aware clamp：ticker 用既有 public BitfinexREST（共用
            # FundingRateLimiter；~2 call/90s ≪ 30/min budget）。預設
            # observe-only（BFX_CLAMP_ENABLED=false）：抓 ticker、log
            # clamp_would_adjust，submit 與 sweep 行為 = 現狀。
            ticker_source=bitfinex,
            clamp=clamp_policy_from_env(os.environ),
```

- [ ] **Step 2: Run full gates**

Run: `cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check`
Expected: 全綠（daemon wiring 由既有 boot/daemon 測試覆蓋建構路徑；若有 daemon 建構測試因新參數失敗，跟上簽名）

備註：compose 不用改 — `bfx-bot` 走 `env_file: .env.runtime`（`~/bfx/bot.env` + `deploy/vm/canary.env` 組裝），未設定的 env 自動吃程式預設。enable 時機見 Task 5。

- [ ] **Step 3: Update standing_quote.py docstring**

module docstring（`standing_quote.py:1-7`）尾端補一段：

```python
"""StandingQuote — per-cell standing intent produced by the signal layer.

Decoupling (spec 2026-05-29): the signal layer (1h candle boundary) decides
the *terms* (POST{rate,period} / SKIP) and writes a StandingQuote here. The
deployment reconciler (90s) reads active (POST + non-expired) quotes and
deploys idle capital toward them without recomputing the signal.

E2 (book-aware clamp, 2026-07-06): submit 前 deployment 層可在 policy 界內把
rate 對齊 live book（taker / undercut / raise；ARCHITECTURE §4 步驟 7c）。
StandingQuote.rate 仍是 POST/SKIP 閘門與 down-clamp floor 的權威 — clamp
只調執行價，不回寫 quote。
"""
```

- [ ] **Step 4: Update ARCHITECTURE.md**

§4 的 7b 段之後（`ARCHITECTURE.md:239` 之後）加一步：

```markdown
7c. **Book-aware clamp（E2）**：步驟 7 建 DecisionPayload 前，每 symbol 每 tick 抓一次 public funding ticker（`GET /v2/ticker/f{sym}`，免認證，共用 FundingRateLimiter），把 quote rate 對齊 live book：`BID ≥ quote.rate` 且 `bid_size ≥ amount` 且 `bid_period ≤ BFX_CLAMP_TAKER_MAX_PERIOD_D` → 掛 quote.rate 直接吃單（taker，唯一保證成交路徑；fill 繼承 bid 的 rate/period）；否則掛 `ASK − 1 tick`（搶隊首），但競爭價低於 `quote.rate × (1 − BFX_CLAMP_MAX_DOWN_PCT)` 時維持 quote.rate（深度下移屬 signal 層職權）。ticker 抓失敗 → 全 fallback quote.rate（= 現狀）。`BFX_CLAMP_ENABLED=false`（預設）僅 log `clamp_would_adjust`。clamp enabled 時 7b 的 sweep ref 同步對齊 `max(quote ref, ASK − 1 tick)` — sustained spike 中不自砍 E2 剛掛的高價單。
```

§4 參數總覽表加一列：

```markdown
| Book clamp（E2） | observe（enabled=false）；down floor 15%；taker ≤7d | `BFX_CLAMP_ENABLED`、`BFX_CLAMP_MAX_DOWN_PCT`、`BFX_CLAMP_TAKER_MAX_PERIOD_D` |
```

§9 的 I-RP 之後（`ARCHITECTURE.md:397` 之後）加一條 invariant：

```markdown
- **I-BC book-clamp bounded**：execution clamp 只調 submit rate — POST/SKIP、period、amount 權威不變（signal gate + sizing 不動）、不回寫 quote、不碰 ledger/tracker。taker 需 `bid ≥ quote.rate`（fill ≥ signal floor）+ `bid_size ≥ amount` + `bid_period ≤ 上限`；down-clamp 以 `BFX_CLAMP_MAX_DOWN_PCT` 為界（超界回 quote.rate）；up-clamp 無上界（風險由 I-RP sweep 收斂，閒置有界 ~min-age）。ticker 失敗 fail-closed（行為 = 無 clamp）、fetch 錯誤不得擋 deploy。clamp enabled 時 sweep ref = `max(quote ref, ASK − 1 tick)`（防自砍）。
```

備註（驗證過、不需改 code）：`DivergenceReporter` 比的是 live vs replay 的 signal direction，從不看 submit rate — clamp 不會誤報；`live_attribution` active arm 只用 venue `fill_rate`、passive baseline 用 candle close — clamp 效果會自然進 primary verdict，`mr_alpha_spread` 的語意變成「timing + execution 混合」（reported-only、never gating；單獨量測 execution alpha 是 E3 的事）。

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "✨ Feat: E2 daemon wiring（BFX_CLAMP_* env，預設 observe-only）+ 📝 ARCHITECTURE §4 7c/§9 I-BC + standing_quote docstring"
```

---

### Task 5: Canary rollout（人工 gate — 不可由 subagent 執行）

- [ ] **Step 1: Observe mode 部署**：merge + push 後照現行 VM 部署流程 redeploy `bfx-bot`（`BFX_CLAMP_ENABLED` 不設 = false）。
- [ ] **Step 2: 觀察 ≥24-48h**：`docker logs bfx-bot 2>&1 | grep -E "clamp_would_adjust|clamp_ticker_fetch_failed"` —
  - branch 分佈合理：`raise`/`undercut` 應為多數（quote 是 65min 前的 close，幾乎永遠偏離 book）；`taker` 罕見（bid 越過 quote = spike 才有）；`floor` 罕見（>15% 下移）。
  - `clamped` 與 `quote_rate` 的差距分佈：普遍 >2x → 先查 ticker 解析/單位再 enable。
  - `clamp_ticker_fetch_failed` 偶發可容忍；連續出現 = rate limit 或 endpoint 問題，暫緩 enable。
- [ ] **Step 3: Enable 順序**：**建議先把 E1 翻 `BFX_REPRICE_ENABLED=true` 並穩定 ≥1 週再開 E2** — I-BC 的 up-clamp 風險上界依賴 I-RP sweep 收斂（E1 未 enable 時，掛太高的單只剩 65min quote TTL 後變 free spike option 這條慢路徑）。Enable = `deploy/vm/canary.env` 加 `BFX_CLAMP_ENABLED=true`（env_file 注入，compose 不用改）→ redeploy。
- [ ] **Step 4: 成功判準（1-2 週）**：`clamp_applied` 正常出現且 submit→fill 延遲（`deployment_submitted` → WS foc EXECUTED）分佈下降；`reprice_cancelled` 頻率不升（自砍防護有效）；taker fill 的 period 全部 ≤ `BFX_CLAMP_TAKER_MAX_PERIOD_D`；G3 報告的 `mr_alpha_spread` 趨勢轉正（語意已是 timing+execution 混合，正式 execution alpha 量測等 E3）。
- [ ] **Rollback**：從 `deploy/vm/canary.env` 移除 `BFX_CLAMP_ENABLED` → redeploy 即回 observe（程式碼可留）。

---

## Self-Review 紀錄（writing-plans 時已跑）

- **Spec 覆蓋**（review §1 E2 逐項）：ticker fetch（每 90s tick、一 symbol 一 call、走 FundingRateLimiter）= Task 1+3；overshoot `min(quote, best_ask − 1 tick)` = UNDERCUT 分支；undershoot `max(quote, market)` = RAISE 分支（market := ask − 1 tick，quote.rate 是 max 的 floor 引數 — review「MR quote 仍是閘門 + floor」的 floor 語意）；taker branch（`BID ≥ quote.rate` → 掛 quote.rate 吃單）= TAKER 分支含 bid_size/bid_period 上限（review 的 BID_PERIOD 警告落地為 `taker_max_period_days` guard，比 review 只「注意」更嚴）；fetch 失敗 fallback = FALLBACK 分支 + reconciler try/except；layer boundary 文件（standing_quote docstring + ARCHITECTURE §4）= Task 4；divergence/attribution 語意驗證 = Task 4 備註（研究確認零 code 變更）。
- **與 review 的刻意差異**：(1) down-clamp 加了 `max_down_pct` floor（review 的 `min()` 無界）— 理由：up/down 風險不對稱（up 的最壞是有界閒置、down 的最壞是 2 天不可逆爛價成交），且 MR 策略本身押注 reversion，深跌時 execution 層不應替 signal 層做 capitulate 決策。(2) taker 加 `bid_size ≥ amount` guard（review 未提）— 防 partial taker fill 後殘量以低於市場的 quote.rate rest。(3) **sweep ref 對齊（E1×E2 交互）是本 plan 新增的必要設計** — review 未涵蓋：無此對齊，sustained spike 中 E1 會每 min_age 週期自砍 E2 剛掛的 ask−tick 高價單（cancel/repost churn）。
- **Placeholder 掃描**：無 TBD/「適當處理」；所有 code step 附完整 code。
- **型別一致**：`FundingTicker` 欄位在 Task 1/2/3 一致；`clamp_rate` keyword-only 簽名 Task 2 定義 = Task 3 呼叫；`ClampPolicy`/`clamp_policy_from_env` 名稱 Task 2 = Task 3 測試 = Task 4 wiring；`book_competitive: float | None` 貫穿 Task 3 (d)/(e)/(g)；`_build` 新參數簽名 = 測試呼叫。`clamp_policy_from_env` 刻意不叫 `policy_from_env`（與 E1 `reprice.policy_from_env` 同檔 import 衝突）。
- **Byte-identical 保證鏈**：`clamp=None`（未 wire）→ 不 fetch、不 clamp、sweep ref 不變；`clamp` observe → 多 fetch + log，submit/sweep 不變；enabled 才有行為差 — 三態都有測試釘住。
- **多 agent adversarial verify（2026-07-06，5 verifiers 對 codebase 實測）已跑並修正**：(1) blocker — rounding 測試原值 0.00025 會誤觸 FLOOR（12/13 PASS），改 0.00021 + 釘 branch；(2) rest.py / reconciler.py / test_reconciler.py / daemon.py 四處 import 插入位置改為明確 isort 排序（repo ruff 啟用 `I` 規則，放錯 I001 擋 gate）；(3) sweep-align 測試的未用 `ex` 解包改 `_`（RUF059）。Verifier 已實跑 plan 的實作 + 測試 + repo ruff/mypy 設定確認修正後全綠。
