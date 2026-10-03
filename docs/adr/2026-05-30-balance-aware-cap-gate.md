---
title: Balance-aware cap gate — 把送單量 clamp 到 funding-wallet available
date: 2026-05-30
status: "superseded-by: [2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md)"
tags: [bfx-funding-bot, decision, deployment-reconciler, risk-management, bitfinex]
related-commits:
  - "c4a6712^..dfa51ee"
---

# Balance-aware cap gate — 把送單量 clamp 到 funding-wallet available

## Context

2026-09-19：本決策的固定buffer／in-memory authority／cap來源已由 [2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md) 取代；下文保留歷史選擇與當時驗收，並非新版本已部署。action-size clamp與送單前資金檢查原則延續。

2026-05-29 canary 真錢故障。deployment reconciler（2026-05-29 standing-quote 設計）只從 policy 算 `gap = cap − exposure`，`external/bitfinex/` 完全無 wallet-balance 讀取。當 cap 略大於實際 funding-wallet 總額，一筆接近 venue 最低下單額的 credit 到期使 `realized` 下降後，reconciler 每 90s 想補 `cap − realized`（略高於最低下單額）卻只有略少於此的 free 餘額 → venue 回 `10001 not enough balance` → **90s 失敗送單迴圈**（真錢安全但約一筆最低下單額的資金閒置 + log 洗版）。當日 stopgap 把 cap 調低（`3a2c731`）止血（gap < min_fill 153 → 休眠）。本決策以真修法取代 stopgap 並還原 cap。

## Options Considered

**強制機制（在哪擋超額單）：**
- A. 靠 venue `10001` 拒單（status quo）— 就是這個噪音迴圈
- B. 只在 sizing clamp（`gap = min(cap−exposure, available−buffer)`）
- C. 只加 per-offer guard（`BuyingPowerGuard`）
- D（選用）. 雙層：sizing clamp（primary，cumulative 正確性）+ per-offer guard（defense-in-depth）

**buffer 形式：** A. 百分比（如 2%）　B（選用）. 固定額 `BFX_BALANCE_BUFFER_USDT` 預設 3

**`available` 持久化：** A. 寫 `position_state`（PG column + alembic migration）　B（選用）. 純 in-memory（經 `PositionReconciled` event → ledger）

**clamp 對象：** A. 壓 risk intent（降 target / concentration cap）　B（選用）. 壓 action size（gap），concentration cap 仍綁 policy cap

## Decision

- **D1 強制機制** = 雙層（sizing clamp primary + `BuyingPowerGuard` backstop），共用單一 bound `available − buffer`
- **D2 buffer** = 固定 `BFX_BALANCE_BUFFER_USDT` 預設 3
- **D3 持久化** = `available` 純 in-memory（無 PG column / migration）
- **D4 clamp 對象** = action size（gap），非 risk intent
- 支撐：available 在既有 reconcile pass 一起抓（非 side-channel）；fetch 失敗 → 跳過該 tick deploy（fail-closed）；gate **live-only**（`DeploymentReconciler` 只在 `if not spec.is_simulated` 建構，guard 在 sim inert 因 chain 唯一 evaluator 是 `reconciler.deploy()`）

## Rationale

- **D1**：sizing clamp 是精準的累積控制（`sum(fills) ≤ available−buffer`），單獨即修好 bug；guard 是 per-offer sanity backstop，讓超額單在 stale snapshot 下也不離開 process。不選「只靠 venue 拒單」= 把 venue rejection 當唯一防線正是噪音迴圈來源；不選「只 guard」= 無累積保證。代價 = 兩處共用同一 buffer（D2 確保只扣一次，非雙重扣除）。
- **D2**：固定額涵蓋 rounding/precision/fees（venue `available` 已扣 holds）。不選百分比：以當時資金規模，2% 約為固定 buffer 的近 4 倍，白白浪費閒置利息；且與既有 `min_offer_buffer_pct`（算 venue minimum 153）語意不同，混用易混淆。代價 = 入金量級大變時需手調。
- **D3**：`available` 是每 tick 重抓的 observed state、非 accounting truth，warm-start 無價值。不選 PG persist：徒增 migration + 投影雙算風險（reconcile 是 snapshot 非 delta event）；in-memory boot 預設 0 = fail-closed。代價 = 重啟後到首次 reconcile（~90s 內有 boot reconcile）暫不部署（安全）。
- **D4**：policy cap / concentration 是 risk intent（operator 選的上界）；available 是當下能部署多少。clamp action size 保持 `cap_per_cell = concentration_pct × allocation_cap`（policy）不被餘額污染。不選壓 risk intent：會讓「暫時缺錢」永久改寫風險設定。

## Result

- `git log --oneline c4a6712^..dfa51ee`（8 commits，TDD 9-task subagent-driven + spec/quality 雙 review）
- 879 unit + mypy(119) + ruff 全綠；最終 3-lens 對抗 review（integration/correctness/safety）全 pass、0 critical/important
- 部署 Koyeb `b5edfa23` HEALTHY；live 驗證 `reconcile_complete … realized=<…> available=<…>`（available= 欄位首現 = 新碼生效）、headroom < min_fill 153 → 乾淨休眠、0 submit / 0 個 10001 / reserved-realized 不變。stopgap `9c8d631c` 退役。

## Followup

3-lens review minor（非阻塞）4 項**全數已收 ✅**：2026-05-30 收 3 項，第 4 項已於 06-01~02 fUSD Stage 1/NAV-split 順手解決（2026-08-16 訂正：先前「第 4 項未收、research-gated 暫緩」的 review 註記是誤植，本專案從未 research-gated）：

- ✅ **#1 done**（`7d54d39`）：BuyingPowerGuard 顯式 `if not spec.is_simulated` gate；executor spec 上移到 guard chain 前以利分支組裝。live-only 契約 local 化、行為中性（**需手動 canary redeploy 才反映**，Koyeb auto-deploy 已停）
- ✅ **#2 done**（`68fb4fe`）：stranded-capital log 在 headroom < policy gap 時改報 `balance-limited`，不再把餘額限制誤記成 concentration（純診斷）
- ✅ **#3 done**（`690141c`）：float→str→Decimal bridge invariant test 釘住 operating-magnitude 無損（含小數參數給 teeth）；不動 `offer_amount_usdt` schema 型別（改 Decimal 牽動 8+ 消費者、對 latent fragility 過重）
- ✅ **#4 done — 單幣別 `available_balance()`**（原描述：`_fetch_available` 取 `first_cell.symbol[1:]`、reconciler 套單一 global headroom）：已隨 fUSD Stage 1/NAV-split（06-01~02）順手解決，`available_balance(self, symbol: str)`/`_fetch_available(self, symbol)` 現為 per-symbol 簽名（`boot_recovery.py:452`），`cells.canary.yaml` 的 fUSD dry-run cell 驗證 per-currency balance gate 正確運作（per-symbol balance gate hard-blocks every fUSD offer）。
- deferred non-goals：USDT/USD ticker fetch（`effective_min` 仍靜態 153）、PG persist（warm-start 用途出現再評估）

## Lessons

### Rules
- **R1**：policy limit（risk intent，operator 選的上界）與 physical constraint（當下 available）混在同一個 sizing 算式會讓「暫時缺錢」改寫風險設定。`Rule: 控制器補 hard constraint 時 clamp 送單量（action size），別動 target / cap`。

### Observations
- **O1**：把 venue rejection（`10001`）當唯一 balance 防線 → 每 tick 撞牆的噪音迴圈；hard constraint 該在送單前 fail-closed 本地擋，venue 拒單只當最後保險。

## Related

- 壓縮來源：spec `2026-05-30-balance-aware-cap-gate-design.md`、plan `2026-05-30-balance-aware-cap-gate.md`（原文已不在 repo，本 ADR 即紀錄）
- 前序設計（repo docs）：deployment reconciler standing-quote、credit-aware reconcile v2（`docs/superpowers/specs/2026-05-29-*`）
