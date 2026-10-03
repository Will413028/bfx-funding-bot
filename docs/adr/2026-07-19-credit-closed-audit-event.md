---
title: CREDIT_CLOSED audit-only 事件 — attribution 的 credit 生命週期真值（Option B）
date: 2026-07-19
status: active
tags: [bfx-funding-bot, decision, event-sourcing, attribution, websocket, bitfinex]
related-commits:
  - c6d1fa5
  - e4e6366
---

# CREDIT_CLOSED audit-only 事件 — attribution 的 credit 生命週期真值

## Context

首份 G3 報告（2026-07-19，weekly chain 修復後）verdict UNRELIABLE：deployment anchor
attributed 約為 observed 的 1.58 倍。根因分毫實證：借款人 40 分鐘內提前還款、bot 同額
重放，credit 關閉無事件路徑 → `open_principal_at` 重複計（attributed 減去該筆 fill 金額
恰等於 observed）；連帶 `fill_duration_days` 把短命 credit 記滿 held-to-term 利息。
**Prior state**：[05-29 ADR](2026-05-29-credit-aware-reconcile-v2.md) 刻意讓 realized 由
reconcile snapshot 獨佔寫入、`fcc` 未解析（4.4a 判準「credit 物件無 offer id」→ no-op）——
量測層卻 silent 依賴它從未取得的 release 資料。G3 gate（cap 加碼／B3／放大金額）全被擋。

## Options Considered

- **A. weekly chain 拉 venue `/credits/hist` REST**：離線取 credit 生命週期，零 live 改動。
- **B. bot 解析 `fcc` → `CREDIT_CLOSED` 域事件（選用）**：close 真值進 event_log SoT。
- **C. 從 `reconcile_observation` realized 遞減差分合成 close**：無新資料源。
- **D. 放寬 anchor tolerance**：否決——掩蓋真實訊號，UNRELIABLE 本來就是對的警報。

**Sub-decision（投影邊界）**：新事件 audit-only（零 ledger/claims 投影）vs 接進 ledger。
**Sub-decision（join 位置）**：離線消費端 join vs live fcn↔fill 關聯狀態機。

## Decision

- **D1**：選 B——`fcc` parse（`FccEvent`）→ dispatcher 發 `CREDIT_CLOSED`。
- **D2**：audit-only：`_AUDIT_ONLY_TYPES` 在 live append 與 rebuild tail fold **兩處** skip。
- **D3**：join 放離線消費端：`apply_credit_closes` 純函式（credit_id 去重、amount 精確 +
  `mts_create` ±5min skew slack、與既有 release 取 min），G3 與 weekly attribution 共用。

## Rationale

- **B 而非 A**：close 進 event_log 讓所有現在與未來的量測消費端共用一份真值；A 每次報告都
  要打 auth REST（分頁/率限），且 credit↔fill 匹配邏輯一樣跑不掉，只是搬到更晚的時點。B 的
  邊際成本低——4.4a 已有 fcn/fcu/foc parse 骨架。代價：live path 多一條 parse 分支（由
  audit-only 邊界把血 radius 壓到零 ledger 影響）。
- **B 而非 C**：90s 快照粒度的 realized 差分在多 credit 並存時無法歸屬單一 credit，精度
  不足以修 anchor。
- **D2（audit-only）**：保 05-29 single-writer 不變式（realized 只由 reconcile 寫），
  **不需重開該 ADR**；相較接進 ledger，放棄「事件驅動 realized 遞減」但那正是 05-29 已
  裁決不要的東西。
- **D3（離線 join）**：venue credit 物件無 offer id，live 關聯需跨事件狀態機（fcn 時間
  相關性）；離線純函式可測、錯了重算即可，量測層本來就是離線批次。

## Result

- 核心 `git log --oneline e4e6366..c6d1fa5`；部署批次收在 `d6e6691`（同日後續 deploy 疊加至 `b1cd87a`）。
- 當日的提前還款案例做成 end-to-end regression fixture（修前 anchor 必 diverge、修後收斂）。
- 全 gates 綠（1420 tests 當批）；store 隔離測試證 append 後 position_state/claims 零觸碰、rebuild byte-identical。

## Followup

- **watch**：下次借款人提前還款應出現首筆 `CREDIT_CLOSED` row（gate 條件：venue 行為，無日期）。
- WS 斷線期間 close 遺失 → degrade 回 held-to-term（接受；anchor 隨事件覆蓋收斂）。
- 歷史不回填：2026-07-19 前的 weekly rows 維持舊假設。
- `fcu`（credit 更新）仍 no-op——period 延長等語意未建模，等有量測需求再議。

## Lessons

### Rules

- **R1**：event_log 的投影是 delta 累加器，「只記錄不投影」的事件型別在 live 投影與
  rebuild tail fold 是兩條獨立路徑——tail 即使零 delta 也會 bump `last_event_seq`。
  `Rule: 新增 audit-only 事件時，兩條路徑同 PR 一起 skip 一起測（byte-identical rebuild 測試為準）。`

### Observations

- **O1**：anchor divergence 差額**正好等於單筆 fill 金額**——離散重複計數（而非模糊模型
  誤差）是「事件缺口」類 bug 的指紋；一筆 SQL 對到分毫即可鎖定根因。

## Related

- 原 plan `2026-07-19-credit-closed-ws-event.md`（commit `c6d1fa5`；原文已不在 repo，本 ADR 即紀錄）
- [2026-05-29-credit-aware-reconcile-v2](2026-05-29-credit-aware-reconcile-v2.md)（已加 Update 註記，不變式未動）
