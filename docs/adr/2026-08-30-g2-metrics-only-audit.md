---
title: G2 calibration 採用 metrics-only audit 與 read-only soak checkpoint
date: 2026-08-30
status: active
tags: [bfx-funding-bot, decision, g2, observability, deployment, safety]
related-commits:
  - "7d77cfd^..114919c"
  - 017a3ce
---

# G2 calibration 採用 metrics-only audit 與 read-only soak checkpoint

## Context

G2/G3 execution-integrity 已有 fail-closed execution gate，但仍缺少可重播的
M1/M2/M3 measurement path，以及不干擾 live canary 的 VM observation path。
既有 aggregate-only WFO JSON 無法回答相鄰 window 的 parameter drift。

## Options Considered

### Audit semantics

- **A. 自動判定 PASS/FAIL 並控制 live state**：自動化較高，但會把尚未定義的 threshold 直接變成交易控制。
- **B. Metrics-only audit（選用）**：保留 evidence 與 `not_evaluated`，代價是仍需人工決定 threshold 與 verdict。

### WFO evidence

- **A. 維持 aggregate-only output**：相容性最好，但無法計算 window-level drift。
- **B. 增加 window manifest（選用）**：增加輸出內容，但保留 aggregate output 並讓 M2 可驗證。

### VM observation

- **A. systemd timer 或 mutation-capable runbook**：可自動週期執行，但增加對 live state 的操作風險。
- **B. One-shot read-only checkpoint（選用）**：安全邊界清楚，代價是 operator 必須手動執行。

## Decision

- **D1**：G2 audit 只讀 structured logs 與 optional manifest；thresholds 為 null，decision 固定 `not_evaluated`。
- **D2**：WFO aggregate result 保持相容，另輸出每個 window 的 identity、status 與 JSON-safe `best_params`。
- **D3**：VM checkpoint 使用 bounded read-only probes 與絕對 report path，不啟用 timer、不呼叫 halt/resume、不修改 compose、git 或 DB。

## Rationale

- **D1**：相較自動 verdict，metrics-only 放棄即時控制，換取不把未定義 threshold 偽裝成證據，也避免 audit 意外改變交易狀態。
- **D2**：相較 aggregate-only，window manifest 增加 schema 與輸出大小，但能辨識 parameter drift；缺資料維持 unavailable，而非被吸收成零。
- **D3**：相較 timer 或 mutation-capable script，one-shot checkpoint 犧牲自動化，換取 funded/idle canary 都能安全觀察且不引入新的控制面。

## Result

- 以 `git log --oneline 7d77cfd^..114919c` 取回 implementation lineage，並由 merge commit `017a3ce` 合併至 main。
- focused tests：`37 passed`；merge 後 full gate：`1900 passed, 86 deselected`、mypy 183 files、ruff clean、shell syntax clean。
- 程式碼已 merge/push；OCI deployment、timer enablement 與 G2 verdict 刻意保留為待後續驗證。

## Followup

- 將 audit/checkpoint 部署至 OCI，取得實際 log window 與 WFO manifest。
- 依累積 evidence 另行定義 M1/M2/M3 thresholds 與 G2 verdict。
- G3 account-migration window review 仍待執行。

## Invariants

- `unavailable` 不得轉成 zero、green 或 PASS。
- Audit 與 checkpoint 不得修改 trading state。
- Read-only checkpoint 不得呼叫 halt/resume、git pull 或 compose mutation。

## Revocation Triggers

- 若 metrics-only evidence 無法支援 operator 判讀，重新評估獨立的 read-only verdict command。
- 若需要週期性自動觀察，重新評估 timer，但維持 mutation-free boundary。
- 若 WFO manifest 與 aggregate output 出現不一致，停止使用 M2 並重評 schema contract。

## Lessons

### Rules

- **R1**：`Rule: evidence gate 遇到缺資料時必須保留獨立的 unavailable 狀態，不得用零值或預設 verdict 代替。`

## Related

- Existing ADR：[2026-08-23-funding-strategy-execution-integrity](2026-08-23-funding-strategy-execution-integrity.md)
- 偵測層前身（本 ADR 即其延後的 Component D）：[2026-05-31-g2-state-level-divergence-detection](2026-05-31-g2-state-level-divergence-detection.md)
- Implementation：`git show 017a3ce`
- SDD source：`backend_py/docs/superpowers/specs/2026-08-30-g2-metrics-only-audit-design.md` 與 `docs/superpowers/plans/2026-08-30-g2-metrics-only-audit.md` 已依核准移除；內容已完整壓縮於本 ADR。

## Updates (2026-08-30 — OCI canary evidence)

`017a3ce` 已部署至 VM canary；首個 bounded soak checkpoint 為 `scheduler_tick=4/reconcile=4/divergence=0/error=0`，五個 compose services 皆 healthy、restarts=0。G2 M1 四個 cells 的 parity/state match 為 `1.0`，M2 為 `unavailable`（6 個 malformed `best_params`），M3 stale/gap 為 `0`，因此 thresholds/verdict 仍為 `null/not_evaluated`。G3 live report 為 73 fills/10 weekly windows，primary bot-vs-idle 為 `PASS`，但 AlwaysFRR spread 為負，且 raw peak 超過 cap C（over-deploy 約 28%）；依既有 cap gate 與 migration review gate，維持目前 caps、不 scale-up，待完成 24h L3 與人工 window/over-deploy review。Reports：主機上的證據檔（2026-08-30 g2-audit、g2-wfo-manifest、g3-live-validation 報告）
