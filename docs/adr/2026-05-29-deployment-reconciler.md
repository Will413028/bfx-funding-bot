---
title: Deployment reconciler — signal/deployment 解耦，reconcile 成唯一送單者朝 target_exposure 收斂
date: 2026-05-29
status: active
tags: [bfx-funding-bot, decision, architecture, lending, reconcile, canary]
related-commits:
  - "983dbae^..b0175a2"   # 7-task 實作
  - 8d42736               # no-ff merge main
  - 5e920a4               # spec
  - bb2a4ac               # plan
---

# Deployment reconciler — signal/deployment 解耦

## Context

canary 真錢營運中，credit-reconcile v2 修好帳本後仍有約一筆最低下單額的資金閒置未重投。調查兩個耦合缺陷：(1) `reference_amount_usdt=150.0` 的 fUST 折 USD < $150，被 Bitfinex `10001`「最低 $150 USD」**靜默拒單**（送單前無 minimum guard，事後才從 venue body 知道）；(2) 重投唯一觸發點是 1h scheduler（`signal_engine.process_candle` 無條件 `executor.submit`），reconcile(90s) 只更新 ledger 不送單 → 資金釋出後最長閒置 ~1h（design gap）。共同根因：「決定要不要放（signal）」與「把資金部署出去（deployment）」綁在同一條 1h 同步鏈，且部署層無金額守門。prior state：1h scheduler→signal→submit 的 1:1 同步鏈是 Phase 4.1 paper-shadow infra 留下的；reconcile 骨幹是 5/27 [對帳骨幹](2026-05-27-reconcile-as-correctness-backbone.md) + 5/29 [credit-aware v2](2026-05-29-credit-aware-reconcile-v2.md) 演進來的。

## Options Considered

**重投語意（核心）**
- A. **沿用最近 standing decision**：signal 只決定條款、資金一空用 standing quote 補
- B. off-boundary 重算 signal：資金一空就重跑策略評估

**分配政策（2 cell 共用 global pool）**
- A. **greedy emptiest-first + 70% 集中度上限**
- B. equal split（兩 cell 各半）
- C. greedy 無上限

**target_exposure（以錢包總額 W 計）**：A. 0.95W（5% buffer） / B. 約 0.75W / C. W（滿載）

**minimum guard**：A. 靜態 `ceil(150×1.02)=153` / B. 動態 fetch USDT/USD 行情折算

**部署觸發層**：A. 擴充既有 90s reconcile poll / B. WS `foc`/`fcn` 事件驅動

## Decision

- **D1**：採 **target-state reconciliation toward a standing quote**（重投語意 A）—— signal 層（1h）只寫 per-cell `StandingQuote`(POST{rate,period}/SKIP)；deployment 層（擴充 `PeriodicReconcile` 90s）= **唯一送單者**，朝 `target = BFX_ALLOCATION_CAP_USDT` 收斂（`gap = target − (reserved+realized)`），safety_chain gate 一併從 signal 移到 deployment。
- **D2**：分配 = greedy emptiest-first + 70% 集中度上限（分配 A）。
- **D3**：cap = 錢包的 95%（target A）；minimum guard = 靜態 153（guard A）；觸發 = reconcile poll（觸發 A）。
- **D4**：per-cell intent 用 `CellDeploymentTracker`（自送單累加 + 每 reconcile 按比例校正到 venue 全域真相），**不依賴 venue credit→cell 歸屬**。

## Rationale

- **D1**：rate 是慢變量、閒置機會成本即時 → 沿用 standing quote、只在 candle boundary 刷新；off-boundary 重算 signal 既無意義（同根 candle）又有害（band 邊緣抖動）。reconcile 即 deployment reconciler，**不開新 loop**，且與既有 single-writer 原則一致 —— 消除 boundary-submit 與 idle-fill 兩路 racing/雙送。代價：redeploy 延遲 ~90s（vs WS 秒級），但相較現況 ~1h 是巨大改善。
- **D2**：本次目的就是別讓資金閒置 → 分配政策服務最大化利用率。equal split 在某 cell SKIP 時讓其半閒著（違背目的）；無上限 greedy 集中度風險最大。greedy emptiest-first 把 SKIP cell 份額流向 active cell、70% 上限避免全押單一 cell。
- **D3**：cap 0.95W —— canary pipeline 已驗證，「首跑 bounded downside」理由過期，留 5% buffer 給 fee/rounding/de-peg（永遠保留小 buffer 是 best practice，避免最後一筆塞不進）；不選滿載即為保留此 buffer，0.75W 則閒置過多。minimum 靜態 153 —— buffer 吸收 USDT de-peg（假設 ≥0.98），**不選**動態 fetch 因需新 venue call（與 balance fetch 同列 future）；代價 = de-peg 跌破 0.98 才誤拒，屆時調 buffer。poll **不選** WS 因 WS 索引之前踩雷（[對帳骨幹](2026-05-27-reconcile-as-correctness-backbone.md)），先用穩的。
- **D4**：venue credit→cell 歸屬正是 fcn-mapping 老雷（[2026-05-29-credit-aware-reconcile-v2](2026-05-29-credit-aware-reconcile-v2.md)）。用自己送單的 intent + 比例校正，全域 cap 仍由 venue 真相 + `AllocationCapGuard` 嚴格守住、per-cell 只用於分配；代價 = 重啟時 intent=0 集中度暫失效（best-effort，全域 cap 不受影響）。

## Result

- `git log --oneline 983dbae~1..b0175a2`（7-task TDD：StandingQuoteStore → sizing → tracker → reconciler → signal 改寫 → PeriodicReconcile 接線 → daemon 接線）+ merge `8d42736`。
- 846 unit / mypy(120 files) / ruff 全綠；subagent-driven，最終 opus holistic review「Ready to merge」（無 Critical/Important，補了 stranded-capital log）。
- **尚未部署真錢**（Koyeb auto-deploy 已停）—— 見 Followup。
- **2026-05-29 已部署 canary**（Koyeb `f10cf630`，cap 調為錢包的 95%）。部署後 opus 對抗 audit（`w1dasor0w`）7 findings 全修並重部署（`ecbc6521`）：**C1** `CellDeploymentTracker.reconcile_to_total` 原 rescale 到 `reserved+realized`，無歸屬的 realized credit 把 cell 灌過 `cap_per_cell` → headroom 負 → 餓死 cell；改只 rescale **reserved** + clamp（代價：集中度上限只管 pending 資本）。**I1** `persist()` 丟棄 `store.append()` 的 dedup bool → WS 重送事件被重複 publish。另 `ee1754a`：venue 拒單時 `submit` 回 `status="failed"` 而非 raise，reconciler 當成功記 phantom intent → 改記 `deployment_submit_rejected`、不 `record_deploy`。

## Followup

- ✅ **部署 canary（operator 手動）**（2026-05-29 已完成，見 Result）：Koyeb `BFX_ALLOCATION_CAP_USDT` 從 0.75W 調高到 0.95W + 跑 `scripts/deploy-koyeb.sh canary`（確認 script 不覆寫 cap）→ 驗 `deployment_submitted` log / 下個 90s tick 重投 / `10001` 停。**部署前 canary 仍每整點撞 10001、閒置不重投**（婉拒了 interim hotfix 154）。
- **balance-aware cap**（future venue-call）：接 `/v2/auth/r/wallets` 動態算 `target = min(ceiling, balance×(1−buffer))`，跟隨入金/利息複利（與 pre-trade balance gate 共用同一 fetch）。
- **deployment submit 結構化事件**：成功送單只 log.info + PG ReservationClaimed，無 `EventType`-tagged stdout 事件 → dashboard 看不到 deploy。
- **rate sanity band**：v1 deferred（與 quote TTL 冗餘，需 best-ask fetch）。

## Lessons

### Rules
- **R1**：subagent 中途崩潰（API 500）後，先讀未 commit 的 working tree 評估完成度，再決定 SendMessage 續跑 or fresh agent 收尾 —— 不要重跑整個 task。`Rule: implementer 崩潰 → 從 working tree state 接手、針對殘缺處 re-dispatch，而非從頭。`
- **R2**：reviewer 的建議（含建議的測試）本身可能錯 —— quality reviewer 提的 split test 斷言錯（greedy 在 gap < cap_per_cell 只填一個 cell，非兩個）。`Rule: controller 要審查 reviewer 的建議，不照單全收；尤其 reviewer 提的測試斷言要對著實作邏輯驗證。`
- **R3**：C1 被實作者自寫的 `test_reconcile_to_total_scales_up` 當正確行為寫死，846 tests 全綠。`Rule: implementer 的測試只證明「與實作一致」；真錢 state 邏輯合併後仍要獨立對抗 audit。`（`status="failed"` 不 raise 的陷阱是 DI seam 類問題的第二例。）

### Observations
- **O1**：策略門檻 `>=` 而非 `>` —— rate_percentile 在「剛好 75th percentile」仍 POST（一個 fresh agent 對此猜錯，靠經驗式跑策略才修對 fixture）。

## Related

- 冷啟動重播被跳過的 boundary（重啟不再等整點）：[2026-09-23-cold-start-replays-skipped-boundary](2026-09-23-cold-start-replays-skipped-boundary.md)

- 原 spec/plan 已壓縮至本 ADR：`2026-05-29-deployment-reconciler-standing-quote-design.md`、`2026-05-29-deployment-reconciler.md`（原文已不在 repo，本 ADR 即紀錄）。
- 前置 ADR：[2026-05-27-reconcile-as-correctness-backbone](2026-05-27-reconcile-as-correctness-backbone.md)、[2026-05-29-credit-aware-reconcile-v2](2026-05-29-credit-aware-reconcile-v2.md)（本次是其 deployment 層續作）。
