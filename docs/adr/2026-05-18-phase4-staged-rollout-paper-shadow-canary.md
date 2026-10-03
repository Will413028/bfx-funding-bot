---
title: Phase 4 上線路徑 — paper／shadow／first canary 三段同一 phase，三道 gate 分界
date: 2026-05-18
status: active
tags: [bfx-funding-bot, decision, deployment, canary, observability, roadmap]
related-commits:
  - c1ab7f1
  - 5d78b72
---

# Phase 4 上線路徑 — paper／shadow／first canary 三段同一 phase，三道 gate 分界

## Context

Phase 3b-WFO（[2026-05-18-phase3b-walk-forward-over-single-split](2026-05-18-phase3b-walk-forward-over-single-split.md)）留下 RatePercentile（6/6 cells）與 MeanReversion（5/6 cells）兩個候選；要決定從 backtest 走到真錢的路徑怎麼切 phase、用什麼 gate 分段、observability 契約怎麼訂。

**約束**：

- `external` Bitfinex funding 單筆最低約 $150：canary 金額下限。
- `external` Bitfinex 沒有 funding testnet：真錢前唯一的「真實」驗證只能是 live feed 上的模擬（paper／shadow）。
- `inherited` solo 開發、WIP=1：同時開多條 sub-spec 會讓 safety bug 沒人盯；pre-launch 單人至今仍成立。
- `inherited` 當時 Phase 4 honor Go-era 的 per-user worker 設計、hosting 用既有 Axiom：兩者之後都被取代（見 Result）。

## Options Considered

### Phase 切法
- **基準. 逐段 canary、每段有觀察期**：Google SRE Workbook「Canarying Releases」（https://sre.google/workbook/canarying-releases/）與 Freqtrade `dry_run` 先行（https://www.freqtrade.io/en/stable/configuration/）。
- **A. Big-bang**（backtest 直上 full production）
- **B. Single-milestone**（S1–S5 一個 phase）
- **C. Per-stage phase**（每 stage 一 phase）
- **D. Hybrid setup + first canary（選）**：Phase 4 = S1 paper + S2 shadow + S3 first canary（1 strategy × 1 cell、小額 canary 上限，接近 venue 最低下單額）；S4 scale-up／S5 full production 歸 Phase 5。基準的「每段觀察期」以 gate 形式收進 D。

### Sub-spec 切分
- **Per-deliverable（選）**：4.1 paper/shadow infra、4.2 safety + execution、4.3 stability／G2 calibration、4.4 first canary
- Monolith／per-component／per-milestone

### Shadow 階段 gate metric
- **Signal-level match rate（選）** vs **P&L tracking error**

### Observability
- **Hybrid contract + inline（選）**：roadmap 鎖 envelope + 4.1 payload，4.2／4.4 只鎖 minimal contract
- Inline／JIT、OpenTelemetry-first、SLO-driven、一次鎖死全部 payload

## Decision

- **D1 = Phase 切法 D**：Phase 4 結束條件 = G3 pass + Phase 4 results doc；multi-cell／multi-strategy／解 cap 都是 Phase 5。
- **D2 = per-deliverable 四份 sub-spec**；4.3（G2）不併入 4.4（G3）。
- **D3 = 三道 gate，公式寫死、門檻留 sub-spec**：
  - G1（S1→S2）binary smoke：≥1hr 連續 signal logging 無 crash、全部寫入、signal 值落在 3b EDA 區間、dashboard 看得到。
  - G2（S2→S3）：M1 signal-level match rate（rolling 7 天）、M2 rolling param drift（沿用 WFO refit）、M3 無 >5min feed gap；shadow 2–4 週。
  - G3（Phase 4 結束）：M1 `|live_pnl − shadow_projected| / |shadow_projected|`、M2 非演練 kill-switch = 0、M3 cap breach = 0、M4 G2 match rate 持續達標；canary 4–6 週。
  - Fail 處置：G2 M1/M2 fail → halt、改走 Phase 3c；G2 M3／G3 M2–M3 屬 infra／營運事件不中斷 phase；G3 M1 fail → halt + 比對 shadow vs canary。
- **D4 = observability 契約**：所有 event_type 共用的 envelope（timestamp／level／phase／strategy／cell／event_type／correlation_id／payload）；`signal` 用 common + `strategy_attributes` extension；metric 名 `bfx.<phase>.<strategy>.<cell>.<metric>`、cardinality ≤50；Bitfinex API key 出現在 log 即 schema violation。
- **D5 = WIP=1 + idle fill**：shadow 觀察期空檔並行 4.2 dev，預估 ~10 週。
- **D6**：handoff 提到的「12 個 deferred sub-decisions」找不到原始清單，**不還原**，改在每份 sub-spec 開頭主動 surface，避免 confabulate。

## Rationale

- **D1**：S3 canary 的真錢資料是 S4 scale-up 的前提，所以 S1–S3 綁同一 phase 才有完整的「從模擬到真錢」證據鏈。**不選 big-bang**：零真錢驗證，retail 級風險；**不選 single-milestone**：staging 本身是月級工作、spec 過大；**不選 per-stage**：phase 數爆炸、sub-spec 間共用 infra 重複。**代價**：Phase 4 本身跨 ~10 週，gate 邊界要靠 D3 撐住。
- **D2**：solo 認知負荷上限 3–4 份；G2 是 stability 驗證、G3 是真錢驗證，併在一份會模糊 gate 邊界。
- **D3 G2 選 signal-level 而非 P&L**：shadow 不下單、沒有真 P&L 可比，P&L tracking error 沒 ground truth 且少數大單會主導 noise；P&L 留到 G3。**代價**：G2 脫離最終目標（收益），只證明「live 算的跟 backtest 一致」。
- **D4**：trading critical path 上各 sub-spec 自訂 schema 容易 drift；**不選 OTel**：單語言 Python + solo 過重；**不選一次鎖死全部 payload**：4.2／4.4 未設計，硬鎖會 confabulate safety trigger 欄位。
- **D5**：**不選 WIP=2 全程並行**：4.2 safety bug 無人注意的風險；shadow 觀察不是線性工作，sprint 切法不適用。

## Result

- 4.1–4.4 全部落地且遠快於 10 週（4.1 5/18、4.3 5/20、4.2 5/21、4.4a／4.4b 5/23 起）。
- **Gate 實際走向**：G1 與 `paper_smoke_runner` 隨 Axiom 退役整個撤掉（[2026-05-25-event-store-3c-axiom-removal](2026-05-25-event-store-3c-axiom-removal.md)）；G2 門檻沒有在 canary 前訂死（4.3 的 G2 audit 因 Axiom 退役需重設計，見 [2026-05-20-phase4.3-locf-staleness-budget](2026-05-20-phase4.3-locf-staleness-budget.md)），後改為 metrics-only audit（[2026-08-30-g2-metrics-only-audit](2026-08-30-g2-metrics-only-audit.md)）；G3 M1 改成 rate-spread attribution 並重框為 bot-vs-idle（[2026-05-30-g3-live-pnl-tracking-error](2026-05-30-g3-live-pnl-tracking-error.md)、[2026-05-31-g3-bot-vs-idle-reframe](2026-05-31-g3-bot-vs-idle-reframe.md)）。
- D4 的 Axiom hosting 被 Postgres event store 取代（[2026-05-23-postgres-event-store-sot-migration](2026-05-23-postgres-event-store-sot-migration.md)）；envelope／`correlation_id`／`phase` 欄位語意延續。
- `canary` 已不是 `Phase` 值（現行 `paper`／`shadow`／`live`，見 `backend/ARCHITECTURE.md` §8）；真錢放大改由後續 capital／envelope ADR 管（[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md)、[2026-09-25-lending-envelope-replaces-probation-and-account-halt](2026-09-25-lending-envelope-replaces-probation-and-account-halt.md)）。
- 驗證：`git log --oneline c1ab7f1^..b686577 -- docs/superpowers/specs/2026-05-18-phase4-roadmap-design.md`

## Revocation Triggers

- 當 multi-cell／multi-strategy 或解 cap（Phase 5 範圍）要上線 → 以本 ADR 的「一段一 gate」原則重訂 gate，不沿用已撤掉的 G1。

## Lessons

### Observations

- **O1**：gate 公式先寫死、門檻延後的切法讓 sub-spec 能各自前進，但三道 gate 中兩道在實際進度裡被繞過或改造——gate 依附的量測後端（Axiom）一換，gate 就跟著失效。

## Related

- 來源（Provenance）：`2026-05-18-phase4-roadmap-design.md`（首版 `c1ab7f1`、schema lock `5d78b72`）（原文已不在 repo，本 ADR 即紀錄）
- 前置：[2026-05-18-phase3b-walk-forward-over-single-split](2026-05-18-phase3b-walk-forward-over-single-split.md)；4.1 設計：[2026-05-18-phase4.1-live-signal-pipeline](2026-05-18-phase4.1-live-signal-pipeline.md)
