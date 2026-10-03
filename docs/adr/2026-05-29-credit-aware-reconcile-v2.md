---
title: Credit-aware reconcile v2 — 收斂到完整 venue 快照 + single-writer exposure + append-only checkpoint
date: 2026-05-29
status: active
tags: [bfx-funding-bot, decision, reconciliation, event-sourcing, bitfinex, ledger, resilience]
related-commits:
  - "9cbc1e4..f6bb79d"
  - "1efe090"
---

# Credit-aware reconcile v2：收斂完整 venue 快照、single-writer exposure、append-only checkpoint

延續並修正 [2026-05-27-reconcile-as-correctness-backbone](2026-05-27-reconcile-as-correctness-backbone.md)（週期對帳骨幹）。

## Context

5/27 的對帳骨幹解了「reserved 卡在 cap 高位」（offer 從不 release），但 **offers-only reconcile 反向過修**：`compute_recovery_actions` 把任何「本地 CLAIMED、但已不在 `/funding/offers`」的 offer 當 `missing_from_venue` release 掉 —— 但 offer 離開 /offers 有**兩個**原因：(a) 取消/到期（release 正確），(b) **撮合成 credit**（capital 仍借出，release **錯**，應記為 realized）。reconcile **只讀 /offers、從不讀 /credits**，無法區分。

實況（canary，真錢）：帳戶全部資金被 bot 借出成 3 筆 active fUST credit，但 `position_state.realized` 只認其中 2 筆 —— bot 對自己約當時資金 1/3 的部署資本無視，每小時重試一筆過量放貸（venue `10001 not enough balance`，無財務損失但帳本錯、且 credit-return 路徑也壞）。event_log 佐證：兩筆 offer（`<id>`）在 05-26 23:48 重啟（boot reconcile）後 1 分鐘被 release，但其中至少一筆其實撮合成仍 active 的 credit。

更深的根因與 5/27 同源：**正確性不可依賴「從缺席推斷狀態轉移」**。5/27 只為 offer 維度修了，沒修 credit 維度。

**設計 review 又揪出第二個問題（實作前）**：若照 v1 naive 做法（把 `PositionReconciled` 當訊號接上 ledger），會產生 **dual-writer double-count** —— 一筆 orphan offer 會被 ledger 算兩次：一次走 `PositionReconciled`（絕對 set `reserved=Σoffers`，已含該 orphan），一次走 recovery 的 `ReservationClaimed` delta。單元測試用 stub bus 遮住了這個破綻（ledger 沒真的接），只有設計 review + 真 ledger 整合測試才抓得到。

## Options Considered

### 決策一：reconcile 如何「獨佔 exposure」而不與 WS delta 雙寫

**A. 用 `venue_seq` 區分 reconcile-sourced vs WS-sourced 事件**：ledger 只對帶 venue_seq 的事件套 delta。
- 看似最小改動、不動路由。
- 但 **submit 路徑的 `ReservationClaimed`（hot path 下單時的樂觀 reserve）`venue_seq` 也是 None**，與 reconcile-sourced 撞型 → 判別子不可靠，會誤殺正常 hot-path delta。否決。

**B. 發布順序 hack**：把 `PositionReconciled` 排在 recovery 事件**之後**發，讓絕對 set 覆蓋掉 double-count。
- 改動小。
- 但**脆弱**：正確性依賴發布順序，任何重排就壞；且兩次發布之間 ledger 短暫錯誤。對真錢系統不可接受。否決。

**C. 路由分離（single-writer）**：reconcile 的 recovery `ReservationClaimed`/`ReservationReleased` **直接餵給 `OfferRegistry`（FSM）**、不上 ledger 訂閱的 bus；只有 `PositionReconciled`（絕對 set）到 ledger。reconcile 當下 ledger 的 exposure 唯一 writer。
- 結構性消除 double-count（不靠判別子、不靠順序）。
- WS hot-path delta 與 reconcile 絕對 set 在時間上互斥，從不對同一筆 fact 同時開火。
- 代價：BootRecovery 多一個 `offer_registry` 依賴 + `_route_fsm` 分流。**選 C**。

### 決策二：reconcile 結果如何持久化 / 留審計

**A. 把 `PositionReconciled` 當 domain event 寫進 `event_log`**：
- 與既有事件流一致。
- 但 `event_log` 的投影是 **delta 累加器**，absolute-set 事件會污染 rebuild（從創世重放會把絕對值當 delta 疊加 → 數字錯）。需給累加器加特例、且 rebuild 語意變複雜。否決。

**B. In-process 訊號 + 直接覆寫 `position_state`（v1 當天稍早 ship 的版本）**：
- 簡單、不碰 event_log。
- 但**喪失對帳歷史**：只剩最後一次 `last_reconciled_at`，無 append-only 審計；金融系統要能回答「某時刻 venue 說什麼」。

**C. In-process 訊號 + append-only `reconcile_observation` checkpoint + delta-tail rebuild**：每次對帳寫一筆 immutable checkpoint（含 `event_seq_fence`）；`position_state` 仍是即時投影；rebuild = 最新 checkpoint ⊕ `event_seq > fence` 的 domain events。
- gold-standard event-sourcing「snapshot/checkpoint + 增量重放」形狀：保全完整審計、rebuild 又快又正確（不從創世）。
- 代價：多一張表 + migration + rebuild 邏輯改寫。Will 明確要「pre-launch 做到最好」→ **選 C**。

## Decision

**reconcile 收斂到完整 venue 快照**：每次 set `reserved=Σ(active offers)`、`realized=Σ(active credits)`（converge-to-truth，不再 infer-from-absence）。新增 `/funding/credits` 抓取（fail-fast，與 offers 同級）。架構採決策一C（single-writer 路由）+ 決策二C（append-only checkpoint + delta-tail rebuild），並加 credit 維度 drift 訊號：`PeriodicReconcile._tick` 在 `realized/reserved` drift > $0.01 時升 `RECONCILE DEGRADED`（offers-only 訊號對 credit drift 全盲）。

單帳戶 + 單幣別（fUST）invariant 讓 `realized=Σ(all active fUST credits)` 不需 credit→offer 對映即精確；**sub-account 隔離 = 宣告的 next step**（讓此 invariant 嚴格成立）。pre-trade balance gate 延後 follow-on PR。

## Rationale

- **A（決策一）不選**：`venue_seq` 判別子在 submit-path claim 上失效，會誤殺 hot-path 正常 delta —— 用一個會漏的判別子守正確性，比沒守還糟。
- **B（決策一）不選**：靠發布順序保正確性是隱性耦合，重排即壞，且中間態短暫錯誤。對真錢的 single-writer 不變式要**結構性**保證，不是時序巧合。
- **A（決策二）不選**：把 absolute-set 塞進 delta-累加的 event_log 會讓 rebuild 雙重計算 —— 混用兩種語意是架構錯誤。**reconcile 是 snapshot、不是 domain event**。
- **B（決策二）相較 C**：B 省事但丟審計歷史；C 多一張表卻換來「能證明 venue 在某時刻說什麼」+ rebuild 從 checkpoint 起跳（快）。pre-launch（小額 canary）做對的成本 << 規模放大後回頭補 —— 延續本專案「pre-prod pivot 比 post-prod refactor 便宜」原則。
- **業界對照**：交易所訂單簿同步（REST snapshot + 帶 seq 的 WS 增量，fence 之前的 delta 丟棄）；event-sourcing 的 snapshot-as-checkpoint。心態一致：snapshot 保證收斂、stream 是延遲最佳化、**單一 writer 管單一狀態**。
- **fence 正確性**：`event_seq_fence` 在 recovery 事件 append **之後**才取 `max(event_seq)`，故 reconcile-sourced 事件 ≤ fence、被 rebuild 的 tail 排除，不會雙重計算。

## Consequences / Follow-ups

- **已部署 + 驗證**（canary，Koyeb deployment `76f7cd40` HEALTHY）：runtime log `reconcile_complete venue_offers=0 venue_credits=3 reserved=0.00 realized=<帳戶全部資金>`；`position_state`（prod realm/default）`realized_usdt` 等於帳戶全部資金、`n_credits=3`、`last_reconciled_at` 已設；3 筆 `reconcile_observation` checkpoint 寫入；**`10001` 過量放貸噪音切換後歸零**。無 DEGRADED blip —— 正確：boot reconcile 先把 realized 從 2 筆收斂到 3 筆 credit 的總額，DEGRADED 訊號只在 `PeriodicReconcile._tick`，等 periodic tick 跑時 drift 已=0（訊號保留給未來真正的 WS-missed drift）。
- **流程**：design review → best-practice 討論（Will 拍板 gold-standard）→ writing-plans 8-task → subagent-driven TDD（每 task implementer + spec/quality 雙 review）→ FF merge main → 手動 canary deploy → 驗證。816 unit + 1 PG 整合（真 ledger+bus 復現「不 double-count」）pass、mypy/ruff/alembic clean。
- **migration**：`reconcile_observation` 表 + `position_state.{last_reconciled_at,n_credits}`（`a3ae9a60862a`）已套用 Neon production branch。Koyeb `BFX_ALLOCATION_CAP_USDT` 本就等於帳戶資金（deploy script 每次重設）；只修了過期的舊 cap 註解。
- **loose ends（非 blocker）**：(1) `realized_loss_24h`/`drawdown_from_peak` 兩 calibrated guard 接 `_StubPnLSource`（回傳 0）= 空轉，真 PnLLedger 待接（放貸利息恆正、本金有平台保護 → realized loss 正常運作下幾近不可能，故低優先）；(2) **sub-account 隔離**（next step）。
- spec + plan 見文末 `## Related`（原寫「repo 內，保留作實作細節」；原文已不在 repo，本 ADR 即紀錄）。

## Update 2026-07-19 — CREDIT_CLOSED audit-only 事件（不改本 ADR 決策）

首份 G3 報告揭露 anchor divergence：借款人提前還款無事件路徑 → attribution 的
held-to-term 假設對「歸還後短期重放」的本金重複計（實證 attributed − 重複計的本金
= observed，分毫吻合）。修法（Will 拍板 Option B）：auth WS 解析
`fcc` → 發 `CREDIT_CLOSED` **audit-only** 事件（`_AUDIT_ONLY_TYPES`：零 ledger/
claims 投影、rebuild tail fold 一併 skip）供 G3 / weekly attribution 消費
（`apply_credit_closes` join）。**本 ADR 的 single-writer 不變式不受影響**——
realized/reserved 仍由 reconcile snapshot 獨佔寫入，credit 維度的 converge-to-truth
機制原封不動；此事件只讓「量測層」不再瞎猜 credit 生命週期。commit `d6e6691` 批次。

## Amendment (2026-09-27): 補記 v1 spec 的次要 ruling 與後續演進

壓縮 spec/plan 時補上原 ADR 未記的 ruling（決策一、二不變）：

- **credit 維度不加 grace window**：REST lag 可能讓 absolute set 暫時少算剛成交的 credit；接受這個延遲漂移，而非對 credits 另設 grace——最壞結果是一次被 venue 以 `10001` 拒絕的過量 offer（無財務損失），下個 90s tick 即校正。offer 維度沿用既有 `action_grace_ms`。
- **correctness ledger 與 attribution ledger 分離**：exposure 只由 venue snapshot 導出（精確）；per-offer 的 claim/release／fill 事件降為 audit 與 P&L attribution（best-effort，允許缺 offer→credit 連結），不再是 exposure 的依賴。這是「不從缺席推斷轉移」在資料模型上的落點。
- **boot credits fetch fail-fast**：與 offers 同級，任一失敗整輪失敗（boot 起不來、runtime 走 `EXECUTOR DOWN`）。
- **後續演進（非本 ADR 決策，僅供對照）**：2026-09-03 `db43ad6` 起 reconcile 改為 full-account（不套 symbol filter）並把 `VENUE_SNAPSHOT_OBSERVED` 經 `AccountEventWriter` 寫進 `event_log`，`reconcile_observation` 改由該事件導出、含該事件的 stream 從空 projection 重放（見 `backend/ARCHITECTURE.md` §5）。這與決策二「reconcile 不進 event_log」的否決理由（absolute-set 污染 delta rebuild）已不同；是否需要 supersede 本 ADR 決策二待 owner 判定。

## Related

- 來源（Provenance）：
  - `2026-05-29-credit-aware-reconcile-design.md`（原文已不在 repo，本 ADR 即紀錄）
  - `2026-05-29-credit-reconcile-v2-best-practice.md`（原文已不在 repo，本 ADR 即紀錄）
- [2026-05-27-reconcile-as-correctness-backbone](2026-05-27-reconcile-as-correctness-backbone.md) — 本 ADR 延伸並修正的骨幹。
- [2026-07-19-credit-closed-audit-event](2026-07-19-credit-closed-audit-event.md) — CREDIT_CLOSED audit-only 事件。
- [2026-05-29-deployment-reconciler](2026-05-29-deployment-reconciler.md)、[2026-05-30-balance-aware-cap-gate](2026-05-30-balance-aware-cap-gate.md)（延後的 pre-trade balance gate）。
