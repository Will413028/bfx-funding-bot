---
title: G1 C1 Continuity Redesign + Self-Smoke Trigger
date: 2026-05-21
status: draft
phase: 4.1.x
supersedes-section:
  - docs/superpowers/specs/2026-05-18-phase4.1-paper-shadow-infra-design.md#C1
related-spec: docs/superpowers/specs/2026-05-18-phase4.1-paper-shadow-infra-design.md
---

# G1 C1 Continuity Redesign + Self-Smoke Trigger

## Context

Phase 4.1 G1 smoke check（`backend_py/scripts/g1_smoke_check.py`）有 6 個 C1-C6 check 作為 paper→shadow 的 deploy gate。**C1 continuity** 原定義（4.1 spec line 717）：

> 每 5min 桶都至少 1 個 signal event；任兩相鄰 signal 間隔 < 5min

**Meta-test bug**：原 spec 的 cadence 假設與實際 production 配置矛盾。

- `backend_py/configs/cells.yaml` 11 cells **全部 `timeframe: 1h`**
- C2 check 邏輯 `per_hour = {"15m": 4, "30m": 2, "1h": 1}` 也確認每 cell 1hr 內只 emit 1 signal
- 11 signals 分到 12 個 5min 桶 → **鴿籠原理保證至少 1 桶空**
- 即使 11 signals 完美錯開，相鄰間隔最大值至少 (60min - 累積)/10 ≥ 5min（spec 要求 < 5min 必違反）

換言之 C1 原定義在現有 cells 配置下 **mathematically 必 fail**。不是 implementation bug 是 spec bug。

**為什麼現在重設計**：系統還沒正式上線，可以重構成 industry best practice 的形狀而不需考量 backwards compatibility。

## Decision Summary

| Decision | Lock |
|---|---|
| D1 C1 invariant 改用 per-cell max-gap + per-cell recency | Per-job schedule adherence + recency 業界 best practice（Cronitor / Healthchecks.io / Prometheus `absent_over_time`） |
| D2 Tolerance factor | `1.5 × expected_period` — Cronitor / Healthchecks grace time 預設範圍 |
| D3 `expected_period` 來源 | `cells[].timeframe`，map `{"15m": 900, "30m": 1800, "1h": 3600}` 秒 |
| D4 LOCF cells（有 `staleness_budget_hours`）特殊處理 | **不特殊處理** — LOCF 設計就是 stale 也照 emit，cadence 不變 |
| D5 `window_end` semantic | `now`（嚴格模式 (i)），不取 `max(observed_ts) + period` | 配合 self-smoke trigger 後不會 flaky |
| D6 Self-smoke trigger | Daemon 在 paper + duration 設定下，`_run()` finally 後 sleep 30s 跑 G1，container exit = G1 exit |
| D7 Code 位置 | 重構 `scripts/g1_smoke_check.py` 業務邏輯到 `src/bfx_funding_bot/smoke/g1.py` module；scripts/ 變 thin CLI wrapper |
| D8 Restart loop on smoke fail | **保留 default Koyeb restart 行為**（feature 不是 bug — 強制人類查 log）；未來吵起來再加 max_failures backoff |

## New C1 Definition

C1 拆兩個 sub-check：

| Sub-check | 公式 | 偵測 fail mode |
|---|---|---|
| **C1a max-gap** | `max(gap_i) ≤ 1.5 × expected_period`，per cell | 任何 cell 中途長時間斷流 |
| **C1b recency** | `(now − last_signal_ts) ≤ 1.5 × expected_period`，per cell | 跑一陣子後死掉、尾段死 |

Cell 0 signals → fail immediately（最嚴重 fail mode，視為 C1a/C1b 雙 fail）。

Cell 1 signal in window → skip C1a（需 ≥ 2 timestamps 算 gap），仍跑 C1b。

合格條件：所有 cells 同時通過 C1a + C1b。

## Self-Smoke Trigger

### 接入點

`backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py::_run()` 在 finally block 之後（現有 line 689 後）。

### Gating logic

```
if config.phase == "paper" AND config.run_duration_hours is not None:
    執行 self-smoke
else:
    skip
```

Exception case **不需顯式 flag** — daemon `_run()` 內 `except* Exception` re-raise，control flow propagate 過 finally 後直接離開，self-smoke 區段 unreachable。`except* asyncio.CancelledError`（SIGTERM）則 fall through 到 self-smoke，符合「SIGTERM 仍跑 smoke 檢查已 emit 部分」的設計。

### Flow

```
daemon.run() exits
    ↓
finally: axiom flush, http client close, log "daemon_shutdown_complete"
    ↓
if gated:
    log "g1_smoke_starting sleep=30s"
    await asyncio.sleep(30)        # Axiom ingestion catch-up (live latency ~5s, 30s safety margin)
    exit_code = await run_smoke_async(phase="paper", hours=duration, cells=config.cells)
    log "g1_smoke_done exit_code=%d"
    if exit_code != 0:
        sys.exit(exit_code)
```

### Container exit semantics

| 情境 | Container exit code | Koyeb 行為 |
|---|---|---|
| Daemon clean + smoke pass | 0 | healthy, no restart |
| Daemon clean + smoke fail | 1 | unhealthy, default restart loop（**feature** — 強制查 log） |
| Daemon SIGTERM during paper smoke | 0 or 1 | 跑 smoke for 已 emit 部分，pass/fail 決定 exit |
| Daemon exception | 1（exception propagates） | skip smoke，exception 本身就是 fatal |
| phase=shadow / no duration | 由 daemon 本身控制 | skip smoke 整段 |

### 為什麼選 `window_end = now`（嚴格）

替代方案 `window_end = max(observed_ts) + 1×expected_period`（寬鬆）對「smoke 跑太晚」不敏感，但失去 timing discipline。**Self-smoke trigger 把 timing 自動化後**，嚴格模式不再 flaky — daemon exit 後 30s 內必跑 smoke，age 永遠 < 1min 遠小於 90min bound。嚴格模式同時也保留「人為手動跑太晚」時自動 fail 的 fail-safe。

## File Layout（Refactor）

**New module**：

```
backend_py/src/bfx_funding_bot/smoke/
├── __init__.py
├── g1.py              # 從 scripts/g1_smoke_check.py 搬入 + C1 新邏輯
└── eda_ranges.py      # 從 scripts/g1_smoke_eda_ranges.py 搬入
```

**Public API**：

```python
# bfx_funding_bot/smoke/g1.py
async def run_smoke_async(
    phase: str,
    hours: int,
    cells: list[dict],
    only: set[str] | None = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """G1 smoke checks. Returns exit code (0=pass, 1=fail, 2=auth, 3=config)."""

def main_cli() -> None:
    """argparse + yaml load + asyncio.run(run_smoke_async(...))"""
```

**Modified**：

| File | 變動 |
|---|---|
| `backend_py/scripts/g1_smoke_check.py` | 縮成 ~15 行 thin wrapper：`from bfx_funding_bot.smoke.g1 import main_cli; main_cli()` |
| `backend_py/scripts/g1_smoke_eda_ranges.py` | 同上，re-export from module |
| `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py` | `_run()` finally 後加 self-smoke trigger |

不動：`docs/superpowers/plans/2026-05-18-phase4.1-paper-shadow-infra.md`（historical plan 文件 frozen）。

## Algorithm Detail

### APL query

```kql
['{dataset}']
| where ['event_type'] == 'signal' and phase == '{phase}' and _time > ago({hours}h)
| project _time, strategy, cell
| order by strategy asc, cell asc, _time asc
```

不再用 `summarize count() by bin(_time, 5m)` — bucket 邏輯整段刪。

### Python loop

```python
TF_TO_SECONDS = {"15m": 900, "30m": 1800, "1h": 3600}
TOLERANCE = 1.5

async def run_c1_continuity(*, client, phase, hours, cells, now_fn):
    events = await client.query_apl(build_apl_query_c1(phase, client.dataset, hours))
    by_cell: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for e in events:
        by_cell[(e["strategy"], e["cell"])].append(parse_iso(e["_time"]))
    
    window_end = now_fn()
    failures: list[str] = []
    for c in cells:
        key = (c["strategy"], f"{c['symbol']}_{c['period_agg']}")
        bound = TF_TO_SECONDS[c.get("timeframe", "1h")] * TOLERANCE
        ts = sorted(by_cell.get(key, []))
        
        if not ts:
            failures.append(f"{key[0]}:{key[1]} 0 signals")
            continue
        
        # C1a max-gap (skip if only 1 signal)
        if len(ts) >= 2:
            max_gap = max((ts[i+1] - ts[i]).total_seconds() for i in range(len(ts) - 1))
            if max_gap > bound:
                failures.append(f"{key[0]}:{key[1]} max_gap={max_gap:.0f}s > {bound:.0f}s")
        
        # C1b recency
        age = (window_end - ts[-1]).total_seconds()
        if age > bound:
            failures.append(f"{key[0]}:{key[1]} recency={age:.0f}s > {bound:.0f}s")
    
    if not failures:
        return CheckResult(
            "C1: continuity", True,
            detail=f"{len(cells)} cells all within 1.5x cadence",
        )
    return CheckResult("C1: continuity", False, detail="; ".join(failures))
```

`now_fn` 注入是為了 unit test 凍時用，避免引入 `freezegun` dependency。

## Edge Cases

| 情境 | 處理 | 理由 |
|---|---|---|
| Cell 0 signals in window | Fail immediately | 完全沒 emit 是最嚴重 fail mode |
| Cell 1 signal in window | Skip C1a，仍跑 C1b | 1hr / 1h-cell 只 1 個 signal 是正常的 |
| Cell 多 signals 全擠開頭 | C1b 抓 last_ts 距 now > bound | 「跑一半死」case |
| User 跑 smoke 比 daemon exit 晚很久（manual mode） | Recency 全 fail | 正確行為，提醒重跑 smoke |
| LOCF cell（`staleness_budget_hours` 設定的 p30 cells） | 套同樣 1.5× expected_period | LOCF 設計就是 stale 也照 emit |
| Tolerance boundary：gap = 90min 整 | Pass（`≤` 含邊界） | 邊界明確 |
| Tolerance boundary：gap = 90min 1s | Fail | 同上 |
| SIGTERM during paper smoke | Self-smoke 仍跑，檢查到 SIGTERM 為止的 emission | 不浪費已收集 data |
| Smoke fail → Koyeb restart loop | 預設保留，**feature**（強制查 log） | 比 silent exit 0 安全 |

## Test Plan

### A. C1 algorithm unit tests（`tests/smoke/test_g1_c1.py`）

| Test | 餵什麼 | 期望 |
|---|---|---|
| `test_c1_pass_regular_emissions` | 11 cells × 1 signal each within bound | pass |
| `test_c1_fail_max_gap_exceeded` | 1 cell 兩 signal 間隔 91min（1h cell, bound=90min） | fail，detail 點名 cell |
| `test_c1_fail_recency_exceeded` | 1 cell 最後 signal 距 frozen-now 91min | fail |
| `test_c1_fail_zero_signals_for_cell` | 1 cell 完全沒 signal | fail |
| `test_c1_pass_single_signal_within_recency` | 1 cell 只 1 ts，age 30min | pass（skip gap check） |
| `test_c1_pass_locf_cell_same_tolerance` | LOCF p30 cell（有 staleness_budget），餵正常 cadence | pass（無特殊處理） |
| `test_c1_boundary_at_exact_tolerance` | gap = 90min 整 → pass；90min 1s → fail | 邊界正確 |

`now_fn` 注入：`run_smoke_async(..., now_fn=lambda: frozen_now)`，test 傳 frozen datetime。

### B. Self-smoke trigger helper tests（`tests/marketfeed/test_daemon_self_smoke.py`）

抽 helper（exception case 由 control flow 隱式 gate，不收 flag）：

```python
async def _maybe_run_self_smoke(
    *,
    phase: str,
    duration: int | None,
    cells: list[dict],
    smoke_runner: Callable[..., Awaitable[int]],
    sleep_fn: Callable[[float], Awaitable[None]],
) -> int:
    """Returns smoke exit code (0 if skipped)."""
```

5 個 case：

| Test | Setup | 期望 |
|---|---|---|
| `test_self_smoke_runs_when_gated` | phase=paper, duration=1 | smoke_runner called, returns its code |
| `test_self_smoke_skipped_shadow` | phase=shadow | smoke_runner NOT called, returns 0 |
| `test_self_smoke_skipped_no_duration` | duration=None | NOT called |
| `test_self_smoke_propagates_exit_code` | smoke_runner returns 1 | returns 1 |
| `test_self_smoke_sleeps_before_runner` | spy sleep_fn | sleep called with 30 before runner |

Exception path 不單獨測 — 由 daemon `_run()` 既有 `except* Exception: raise` 處理，self-smoke helper 結構上不可達。

### 砍舊測試

`backend_py/tests/scripts/test_g1_smoke_check.py` 內：

- `test_c1_passes_when_all_buckets_non_empty` — **刪**
- `test_c1_fails_with_empty_bucket` — **刪**

其他 c2 / c3 / c5 / c6 / tabular_to_rows tests 不動。

### 不寫的測試

- Daemon 全跑 + 真 Axiom 的 e2e — 太慢且 G1 本身就是 e2e gate
- Koyeb container restart loop 測試 — infra 行為，不在程式內
- `sleep(30)` 真睡 30 秒 — 用 spy 確認 call args 即可

## Supersedes

舊定義（`2026-05-18-phase4.1-paper-shadow-infra-design.md`）：

- Line 717（C1 spec table row）— 加 supersede note 指向本 spec，原文用 strikethrough 保留為歷史
- Line 752+（APL query 範例）— 加 deprecation note
- Line 808（example output）— 加 deprecation note

舊 plan doc（`2026-05-18-phase4.1-paper-shadow-infra.md`）— **不動**，frozen 為歷史紀錄。

## Decision Log

**Why (i) strict not (ii) lenient `window_end`**

(i) 嚴格 = `now`；(ii) 寬鬆 = `max(ts) + 1×expected_period`。
Self-smoke trigger 把執行 timing 自動化後，(i) 不再 flaky（daemon exit 後 30s 內必跑 smoke，age 永遠 < 1min）。
(i) 同時保留「人為手動跑太晚」的 fail-safe — 顯式 fail 比靜默通過安全。
Industry best practice（Prometheus `absent_over_time`、Datadog "no data" alert）也是 wall-clock recency。

**Why (b) module refactor not (a) subprocess**

(a) subprocess = `asyncio.create_subprocess_exec(sys.executable, scripts/g1_smoke_check.py)` — 0 重構成本，多 ~100ms fork。
(b) module refactor = 把 250 行業務邏輯從 `scripts/` 搬到 `src/bfx_funding_bot/smoke/` — long-term cleaner，測試直接 import。
選 (b) 因為「現在還沒上線可以重構成最好做法」；scripts/ 內 ~250 行業務邏輯本來就不該長期住 scripts/。

**Why max-gap + recency only（不加 CV regularity / 不加 lower-bound gap）**

CV regularity：1hr / 1h-cell 只 ~1 個 signal，sample size 不夠算 CV，**本期實質沒用**。
Lower-bound gap：scheduler 已有 idempotency guard（Phase 4.3 ANCHOR_TS check），redundant。
YAGNI — multi-timeframe cells 上線或真踩到 burst bug 再加。

**Why tolerance 1.5× hardcode 不外露**

Cronitor / Healthchecks.io grace time 預設都在 50% 範圍。
未來想調再外露成 config — YAGNI。

**Why restart loop on smoke fail 保留**

Koyeb 預設 unhealthy container 會 restart loop。Smoke fail → exit 1 → restart → 又 1hr → 又 smoke fail = 無限 loop。
**這是 feature** — G1 fail 意思是 daemon 邏輯壞了，每次跑都會壞，restart loop 持續發出 log noise → 人類會被 alerted。
比起靜默 exit 0 過關更安全。
未來吵起來再加 `max_smoke_failures=3` 後 sleep forever — YAGNI。
