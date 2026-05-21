---
title: Phase 4 Roadmap — paper/shadow/canary deployment design
date: 2026-05-18
status: draft
phase: 4
related-adr: ~/second-brain/wiki/projects/bfx-funding-bot/decisions/2026-05-18-phase3b-walk-forward-over-single-split.md
related-results: docs/research/2026-05-18-phase3b-wfo-results.md
---

# Phase 4 Roadmap — Paper / Shadow / Canary Deployment

## Context

Phase 3b-WFO（2026-05-18 完成）兩策略 qualify Phase 4 候選池：

- **RatePercentile** — 6/6 cells qualify
- **MeanReversion** — 5/6 cells qualify（fUST×p30 thin data fail）
- Best cell：MeanReversion × fUSD×a30（margin +19.9%, 81.6% win rate）

Phase 3c（FRR-trend / SpikeDetect / FRR 解碼）OPTIONAL — 兩策略已 qualify，可直接進 Phase 4。

Phase 3a → 3b 累積的方法論教訓（business 沿用）：

- 任何 methodology 決策先 surface 業界 spectrum（[[../../../../second-brain/prompts/atoms/quant-backtest-methodology|prompts/atoms/quant-backtest-methodology]] R1）
- 不自設 binary gate 在連續訊號上（R2）
- Folklore 假設先 EDA 驗證（R3）
- WFO 多窗 OOS 取代 single-split（R4）

## Phase Boundary Decision

**Pattern**: Hybrid setup + first canary

Phase 4 從 Phase 3b 候選池接手，跨 3 個 deployment stage：

| Stage | 內容 | Phase 4 內? |
|---|---|---|
| S1 Paper trading | live feed + simulated orders | ✓ |
| S2 Shadow mode | live signals 但不下單 | ✓ |
| S3 First canary | 真錢小額（$150-$500）單策略單 cell | ✓ |
| S4 Gradual scale-up | 解 cap / 多 cells / 多 strategies | Phase 5 |
| S5 Full production | 多策略多 cells 在 production | Phase 5 |

**Phase 4 結束 = G3 pass + Phase 4 results doc** — 拿到「single strategy × single cell × small allocation × real-money P&L」+ 驗證 live P&L tracking error vs shadow 預測在 acceptable range。

### Rationale (Pattern spectrum)

| Pattern | 怎麼切 | 為何不選 |
|---|---|---|
| 1. Big-bang deploy（backtest → S5 直上）| - | retail / 高風險 |
| 2. Single-milestone（S1-S5 一個 phase）| - | spec 過大、staging 本身月級工作 |
| 3. Per-stage phase（每 stage 一 phase）| - | phase 數爆炸、sub-spec 間共用 infra 重複 |
| **4. Hybrid setup + first canary**（**選**）| Phase 4 = S1+S2+S3，Phase 5 = S4+S5 | 平衡；S3 canary data 是 S4 scale-up 前提 |
| 5. Pause-and-validate（每 stage 後強制觀察期）| - | 可作 Phase 4 內部 gate (S2→S3)，不必獨立 |

## Sub-spec Inventory

4 個 sub-spec（observability 不獨立，併入本 roadmap 一個 section）：

| # | Title | Scope | 預估工程量 |
|---|---|---|---|
| **4.1** | Paper / shadow infra | live market feed 接線、signal logging、無下單 dry path、divergence reporter | 大（1-2 週 dev）|
| **4.2** | Real-money safety + execution gate | dry-run flag、max-position cap、kill-switch、Bitfinex `offer/new` 接線 | 大（1-2 週 dev）|
| **4.3** | Stability validation + G2 calibration | rolling param drift metric、live vs backtest signal divergence metric、G2 threshold proposal doc | 中（3-5 天，需 4.1 shadow data）|
| **4.4** | First canary deployment | 單策略單 cell × small allocation + 4-6 週觀察 + Phase 4 results doc + Phase 5 entry recommendation | 中（deploy + 4-6 週觀察）|

### Rationale (sub-spec 切分 spectrum)

| 派別 | 怎麼做 | 為何不選 |
|---|---|---|
| Monolith（一份大 design doc）| - | phase 跨度 ~10 週，維護成本高 |
| **Per-deliverable**（**選**）| 每個 deliverable 一份 sub-spec | Amazon 6-pager style；solo dev 認知負荷上限 3-4 |
| Per-component | 按 software component 切 | 4.1+4.2 已是 component cut |
| Per-milestone | 按 stage milestone 切 | sub-spec 之間共用 infra 會重複 |
| Fractal（top + sub-spec）| - | 本 roadmap doc 就是 top，下面 sub-spec 是 per-deliverable |

不選「合 4.3 → 4.4」：stability 是 **G2 gate**（4.4 前置），跟 canary deploy（G3）邏輯不同階段，混在一起會模糊 gate 邊界。

## Observability Schema Contract（roadmap-level section）

不獨立 sub-spec — 在本 roadmap 訂 schema + naming convention，4.1/4.2/4.3/4.4 都引用。Hosting 在既有 Axiom 設施（ROADMAP Phase H 已部署，命名跟本 phase 4 不同維度，前者是 Go-era 運維 phase，後者是 rewrite 後 backtest research 系列）。

### Rationale (observability 派別 spectrum)

| 派別 | 怎麼做 | 為何不選 |
|---|---|---|
| Inline / JIT（每 sub-spec 自定）| - | trading critical path schema drift 風險高 |
| Contract-first（Honeycomb school）| 訂 schema + cardinality bound 先寫 | 對；但獨立成 sub-spec 過度結構化 |
| OpenTelemetry first | adopt OTel semantic conventions | single-language Python + solo dev 過重 |
| SLO-driven（Google SRE）| 從 SLI/SLO 反推 metric | 大型 production；v0 過細 |
| **Hybrid contract + inline**（**選**）| shared metric contract first；sub-spec specific inline | solo / 中小團隊實務派 |

### Log Schema (locked 2026-05-18)

#### Schema lock scope decision

**派別**: Envelope + 4.1 payload locked, 4.2/4.4 events 留 minimal contract

| 派別 | 做法 | 為何不選 |
|---|---|---|
| Folded（只訂 4.1 三個 event_type 詳細 payload）| 4.2 開工再回頭改 roadmap | 4.2 brainstorm 時要分散注意力 |
| **Envelope + 4.1 payload**（**選**）| 6 個 envelope 訂死 + 4.1 三個 payload 訂死 + 4.2/4.4 三個 minimal contract | 跨 sub-spec 防 drift 同時 avoid confabulation |
| Wide（6 個全訂）| 一次訂死全部 payload | 4.2/4.4 還沒 design，confabulate safety trigger / order metadata 風險高 |

#### Envelope (6 event_type 共用必填欄位)

| 欄位 | 型別 | 必填 | Note |
|---|---|---|---|
| `timestamp` | ISO 8601 with TZ | ✓ | |
| `level` | enum: `debug` / `info` / `warn` / `error` / `critical` | ✓ | |
| `phase` | enum: `paper` / `shadow` / `canary` | ✓ | |
| `strategy` | enum: `rate_percentile` / `mean_reversion` | conditional | `health_check` event nullable；其他 event 必填 |
| `cell` | string (e.g. `fUSD_a30`, `fUST_a30`) | conditional | `health_check` event nullable；其他 event 必填 |
| `event_type` | enum: `signal` / `signal_divergence` / `decision` / `safety_trigger` / `order_submit` / `order_fill` / `health_check` | ✓ | `signal_divergence` added 2026-05-21 — divergence reports use distinct event_type (orthogonal to severity) so counts of strategy emissions don't double |
| `correlation_id` | UUID | ✓ | 同 decision flow 跨 event 共用 |
| `payload` | object | ✓ | event_type-specific schema |

#### 4.1 Event Payloads (訂死)

##### `signal` payload

**派別**: Common + extension（OpenTelemetry attributes 派）

| 派別 | 做法 | 為何不選 |
|---|---|---|
| Strict typed per strategy | RatePercentile / MeanReversion 各一 typed schema | 加策略要改 schema doc 雙處；跨策略 dashboard 難 |
| **Common + extension**（**選**）| Common fields + `strategy_attributes: {object}` 放策略獨有 | 跨策略 dashboard 可比；不限制策略創新 |
| Free-form `signal_details` object | envelope 固定 + `signal_details: {}` 任策略塞 | cross-strategy 查詢 unfriendly |

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `signal_score` | float | ✓ | 跨策略可比的 signal strength normalized score |
| `signal_direction` | enum: `post` / `skip` | ✓ | 策略建議的 action（純策略 raw output，未經 safety / cap 等 pipeline 過濾）|
| `strategy_attributes` | object | ✓ | 策略獨有欄位（rate_percentile: `{percentile, threshold}`; mean_reversion: `{rate, mean, sigma}`） |

Final action (pipeline 走完後實際下不下單) 由 `decision` event 負責（兩者用 `correlation_id` join），signal event 不重複記。4.1 paper phase 兩者必相等；4.2 safety logic 加入後可能 diverge。

`strategy_attributes` 結構（per strategy）：
- **rate_percentile**: `{ percentile: float, threshold: float }`
- **mean_reversion**: `{ rate: float, mean: float, sigma: float }`
- Phase 3c 新策略加入時 evolve common ground，先以兩策略 driven。

##### `decision` payload

策略無關，dry / shadow / canary 共用。

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `decision_outcome` | enum: `post` / `skip` | ✓ | 是否下單 |
| `signal_correlation_id` | UUID | ✓ | 對應觸發此 decision 的 signal event |
| `offer_rate` | float | post 必填 | Bitfinex funding rate (e.g. `0.0001` = 0.01% daily) |
| `offer_amount_usdt` | float | post 必填 | 下單金額 |
| `offer_duration_days` | int | post 必填 | 2-120 days |
| `skip_reason` | enum: `below_threshold` / `max_position_cap` / `safety_block` / `insufficient_balance` / `other` | skip 必填 | 跳過原因 |
| `skip_reason_detail` | string | optional | free-text 補充（特別給 `other`） |

Note: `abort` / `retry` 不放這 — `abort` 屬 `safety_trigger`，`retry` 屬 `order_submit`。

##### `health_check` payload

Bitfinex WebSocket 健康指標（G2 M3: zero gap > 5min 對應）。

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `check_target` | enum: `bitfinex_ws` / `bitfinex_rest` / `db` / `redis` | ✓ | 健檢目標（4.1 主要用 `bitfinex_ws`；其他預留） |
| `status` | enum: `healthy` / `degraded` / `down` | ✓ | 狀態 |
| `last_msg_age_ms` | int | ws 必填 | 距上一筆 WebSocket message 毫秒（>300000 = 5min gap） |
| `reconnect_count_last_hour` | int | ws 必填 | rolling 1hr reconnect 次數 |
| `latency_ms` | int | rest/db 必填 | RTT 或 query latency |
| `error_message` | string | degraded/down 必填 | 錯誤原因 |

Emit 頻率：**狀態變化 emit + 每 5min 強制 heartbeat**（變化 driven 為主，heartbeat 確保「沒消息」也能查 down 狀態）— 4.1 implementation detail，schema 不訂死。

#### 4.2/4.4 Event Minimal Contracts

4.2/4.4 brainstorm 時 finalize 詳細 payload，roadmap 此處只訂 minimal contract（必含欄位），sub-spec 可加但不可移除。

##### `safety_trigger` minimal contract (4.2 訂死)

- 必含：`trigger_type` / `triggered_by_correlation_id` / `action_taken`
- envelope `level` 強制 ≥ `error`
- 預警類（e.g. cap_breach 80% 預警）走 `warn` level 另一個 event_type（4.2 brainstorm 訂）

##### `order_submit` minimal contract (4.2 訂死，paper trade 也用 with `is_simulated=true`)

- 必含：`offer_id` / `is_simulated` / `offer_rate` / `offer_amount_usdt` / `offer_duration_days` / `submit_status`
- `offer_id`: real mode 用 Bitfinex 回的 ID；paper mode 自產 UUID
- Lifecycle: order_submit → (eventual) order_fill；中間 status 變化（cancelled, expired）由 4.2 brainstorm 決定是否新增 `order_status_change` event_type

##### `order_fill` minimal contract (4.2/4.4 訂死)

- 必含：`offer_id` (join 用) / `is_simulated` / `fill_rate` / `fill_amount_usdt` / `fill_duration_days` / `fill_timestamp` / `pnl_realized`
- 4.4 G3 M1 (P&L tracking error) 計算來源欄位

#### Log Level Guideline

| Level | 何時用 |
|---|---|
| `debug` | verbose tracing；只在 paper phase enable |
| `info` | normal flow（signal / decision / order_* 正常都 info） |
| `warn` | degraded but not blocking（health_check `degraded`、API retry 成功） |
| `error` | operational failure 但 bot 繼續（order_submit failed、db query failed） |
| `critical` | kill-switch / cannot continue（safety_trigger 強制 ≥ critical） |

#### Retention + PII

- **Retention**: Axiom default 30 day；canary phase 60 day（4-6 weeks 觀察期 + buffer）
- **PII**: bfx 是 single-user bot（Will own funding），無 user PII。唯一敏感資料 = Bitfinex API key — **emit code 強制 redact**。API key 出現在 log 即 **schema violation**，CI / smoke test 要 catch。

### Metric Naming Convention

格式：`bfx.<phase>.<strategy>.<cell>.<metric>`

例：
- `bfx.shadow.mean_reversion.fUSD_a30.signal_match_rate`
- `bfx.canary.mean_reversion.fUSD_a30.pnl_tracking_error`
- `bfx.paper.*.health_check_latency_ms`

Cardinality bound: phase × strategy × cell ≤ 50 unique series；超過要 review。

### Axiom Dashboard 約定

每 sub-spec 至少含 1 個 dashboard。Roadmap 只訂 contract（命名 + focus），**具體 widget 列表 defer 到各 sub-spec brainstorm**（widget 需 real data shape driven，spec 級訂死容易 confabulate）。

**命名規則**: `bfx-phase4-<sub-spec-number>-<purpose>`，例如：
- 4.1: `bfx-phase4-4.1-shadow-signal` (shadow signal volume + divergence trend)
- 4.2: `bfx-phase4-4.2-safety-trigger`、`bfx-phase4-4.2-bitfinex-api-latency`
- 4.3: `bfx-phase4-4.3-g2-stability`
- 4.4: `bfx-phase4-4.4-canary-pnl`

## Dependency + Sequencing

```
Roadmap § Observability section（schema + naming + Axiom dashboard contract）
   │ (所有 sub-spec 引用此 contract)
   ▼
4.1 paper/shadow infra ──► shadow run (W3-W4) ──┐
                                                 ├─► 4.3 stability calib ──► 4.4 canary
4.2 safety + exec gate (parallel W3-W4 dev) ────┘    (G2 gate)               (G3 gate)
```

### Timeline（solo dev WIP=1 + idle fill）

```
Week 1-2:  4.1 paper infra dev → smoke test 通過 G1
Week 3-4:  4.1 shadow run（被動觀察）+ 4.2 safety dev 並行
Week 5:    4.2 ship + 4.3 stability calibration（用 W3-W4 shadow data）
Week 5-6:  G2 評估 + 訂 threshold
Week 6-10: 4.4 canary deploy + 4-6 週觀察 + G3 評估
Week 10+:  Phase 4 results doc + Phase 5 entry recommendation
```

總時程 **~10 週**（6-week active dev + 4-week canary observation）。

### Rationale (sequencing spectrum)

| 派別 | 做法 | 為何不選 |
|---|---|---|
| **WIP=1 strict sequential + idle fill**（**選**）| 一次一個 sub-spec，shadow 觀察期 idle time 並行下一個 | solo + real money 標配 |
| WIP=2 全程並行 | 多 sub-spec 同時 dev | 4.2 safety bug 沒被 4.1 注意風險高 |
| Critical-path first then fan-out | longest task 優先 | medium-size org |
| Interleaved（每天輪換）| 多 sub-spec context switch | switching cost 對 solo 過高 |
| Sprint/batch（2-week sprint）| sprint commit 一個 sub-spec | shadow 觀察期不是線性 work |

## Phase Gates

3 道 gate — metric formula 寫死，threshold 數值留 sub-spec brainstorm。

### G1 (S1→S2) — Paper trade infra ready

**Type**: Binary smoke test（不需 numeric threshold）

**Pass criteria**:
- ≥ 1 hour 連續 live signal logging without crash
- All signals 寫進 DB（zero write failures）
- Signal value 落在 Phase 3b EDA 觀察區間 `[min, max]` 內
- Axiom dashboard 看得到 metric stream

**責任 sub-spec**: 4.1（定義測試 setup）+ roadmap observability section（log schema）

### G2 (S2→S3) — Shadow validation pass

Shadow 階段 metric 兩派：

| 派別 | Metric | Pros | Cons |
|---|---|---|---|
| **Signal-level match rate**（**選**）| `(expected_action vs simulated_action match count) / total decisions` | high signal-to-noise；decision-based | 脫離 P&L outcome |
| P&L-level tracking error | 累積 simulated P&L vs backtest projection 偏差 | outcome-based 符合最終目標 | noise 高；少數大 trade dominate |

選 signal-level：shadow 階段沒下單沒真 P&L，P&L tracking error 沒 ground truth 可比；P&L tracking error 留 G3 用。

**Metrics**:
- **M1**: signal-level match rate ≥ TBD% (rolling 7-day window)
- **M2**: rolling param drift Δ ≤ TBD% across WFO refit windows（沿用 Phase 3b WFO 方法做 live data refit）
- **M3**: zero data-feed gap > 5 minutes（Bitfinex WebSocket 健康指標）

**Period**: 2-4 weeks shadow run（4.3 sub-spec 訂死）

**責任 sub-spec**: 4.3 stability validation（calibration + 三個 threshold 訂死）

### G3 (Phase 4 結束) — Canary validation pass

Real money 上線 multi-dimensional validation：

**Metrics**:
- **M1**: cumulative P&L tracking error `|live_pnl - shadow_projected_pnl| / |shadow_projected_pnl|` ≤ TBD% (4-6 weeks canary)
- **M2**: unplanned kill-switch trigger count = 0（planned drill 不計）
- **M3**: allocation cap breach count = 0
- **M4**: G2 signal match rate 持續 ≥ G2 threshold（sanity check，跨 phase 連續 monitor）

**Period**: 4-6 weeks canary

**Allocation cap range**: $150-$500 USDt（Bitfinex funding min order ≈$150 USDt 是下限；上限留 4.4 訂死）

**責任 sub-spec**: 4.4 first canary deployment（threshold + allocation cap + 觀察期 訂死）

### Gate failure handling

| Gate | Fail → 動作 |
|---|---|
| **G1 fail** | 修 paper infra bug，repeat smoke test |
| **G2 fail (M1/M2)** | Halt + 進 Phase 3c FRR 解碼 / SpikeDetect / FRR-trend 候選策略，或 backtest assumption revise |
| **G2 fail (M3)** | infra issue，修 reconnect logic，不算 strategy 問題 |
| **G3 fail (M1)** | Halt + post-mortem，比對 shadow vs canary divergence root cause；可能要回 4.1 改 simulator |
| **G3 fail (M2/M3)** | Operational incident，獨立處理；不必中斷 phase |

## Out of Scope

| 議題 | 理由 |
|---|---|
| Worker model 設計 | `backend_architecture.md` 已 commit Per-User Worker（goroutine → Python asyncio task），Phase 4 honor 既有架構 |
| Phase 5 scale-up | 解 allocation cap / 多 cells / 多 strategies — Phase 5 議題，前提是 G3 pass + canary 累積 stability data |
| Phase 3c（FRR 解碼 / FRR-trend / SpikeDetect）| G2 pass 則延後；G2 fail 才啟動 |
| Multi-strategy 並行 deploy | first canary 限 1 strategy × 1 cell；多策略並行屬 Phase 5 |
| WeekendPremium strategy | Phase 3b ADR D5 永久 drop |
| fUST × p30 cell | Phase 3b-WFO MeanReversion fail（thin data 17.7k candles）；backfill long-term TODO |

## Open Questions（留待 sub-spec brainstorm）

| Q | 解凍 sub-spec |
|---|---|
| First canary 選哪個 strategy × cell（候選：MeanReversion × fUSD×a30 / MeanReversion × fUSD×p2 / RatePercentile × fUST×a30）| 4.4 |
| Shadow 期具體長度（2 vs 4 weeks）+ G2 三個 threshold 具體數值 | 4.3 |
| Canary 期長度（4 vs 6 weeks）+ G3 tracking error threshold + allocation cap 具體值 | 4.4 |
| ~~Observability log schema 具體欄位~~ | ✅ Resolved 2026-05-18（roadmap observability section locked） |
| Axiom dashboard 具體 widget 列表 | 各 sub-spec brainstorm（roadmap 只訂 naming + focus contract） |
| Safety flag 具體實作（feature flag table vs env var vs config）| 4.2 |
| Kill-switch trigger condition 具體定義 | 4.2 |

### 12 deferred sub-decisions 處理

Handoff 提到 CLAUDE.local.md 有 12 deferred sub-decisions，但 repo 內任何檔案都未找到原始 inventory（可能是上 session 對話累積但未落地）。處理策略：

1. **不主動還原** — 強行還原會 confabulate
2. **每個 sub-spec brainstorm 開頭**問「這個 sub-spec 範圍內有沒有上 session 推遲的 sub-decision 要先 surface？」— 自然撈回相關條目
3. 本 roadmap 內留此 placeholder 提醒 sub-spec 階段要 catch

## Risks Register

| ID | Risk | Mitigation |
|---|---|---|
| **R1** | live signal divergence > backtest noise 範圍 → G2 fail | 4.3 calibration 訂寬鬆 threshold；G2 fail 進 Phase 3c fallback |
| **R2** | alembic `psycopg2` import error 沒解（asyncpg 遷移時沒同步）→ 4.2 ORM work block | 4.2 開工 Step 0 必須先修 |
| **R3** | Bitfinex API rate limit / WebSocket disconnect | G2 M3 metric 涵蓋；reconnect logic 在 4.1 設計 |
| **R4** | real money small allocation 的 P&L noise > 信號 → G3 false-fail | 4.4 訂死觀察期下限（4 weeks）並準備延長到 6 weeks |
| **R5** | solo dev cognitive overload | WIP=1 critical-path sequencing 已對應 |
| **R6** | First canary 選錯 cell（e.g. 選 p30 期 carry risk 過高）| 4.4 brainstorm 先 surface cell 選擇 spectrum |

## Phase 5 Entry Condition（roadmap 連續性）

G3 pass + 累積 ≥ 4 weeks canary stability data → Phase 5 entry recommend。Phase 5 spec 在 Phase 4 結束後另外 brainstorm，不在本 roadmap scope。

Phase 5 預期討論：
- Allocation cap 解除策略（一次到位 vs 漸進 2×/4×/8×）
- 多 cells 同時 deploy 順序
- Multi-strategy 並行 vs portfolio weighting
- Live stability monitoring SLO

## Related

- Phase 3b ADR: `~/second-brain/wiki/projects/bfx-funding-bot/decisions/2026-05-18-phase3b-walk-forward-over-single-split.md`
- Phase 3b-WFO results: `docs/research/2026-05-18-phase3b-wfo-results.md`
- Phase 3b-WFO spec（採用版）: `docs/superpowers/specs/2026-05-18-phase3b-wfo-strategy-matrix-design.md`
- Backend architecture（worker model source of truth）: `backend_architecture.md`
- ROADMAP（Phase A-L Go-era legacy）: `ROADMAP.md`
- Strategy spec: `strategy_specification.md`
