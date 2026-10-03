---
title: 第 3 級自動停機在條件消失後自動恢復（有次數上限），operator kill 除外
date: 2026-09-26
status: active
tags: [bfx-funding-bot, decision, safety, trading-state]
---

# 第 3 級自動停機在條件消失後自動恢復

## Context

prior state：[條款包絡 ADR](2026-09-25-lending-envelope-replaces-probation-and-account-halt.md) D3 第 3 級（受管 offer
金額不符、identity conflict、送單節流持續觸發、無法解釋的 lent＞ledger、無法分類的承諾）寫 `HALTED/auto`，
planner 按 id 撤受管 offer；D4 規定只有 operator 以 TOTP resume 能結束 HALTED，DB trigger
`guard_trading_state_transition`（migration `5b1e7c9d2a40`）拒絕 cause≠operator 的 → ACTIVE。
2026-09-26 cutover 時 Will 指出需求是自動放貸：自動停機後要能自動恢復。

**約束**：

- `external` 只有一名 operator（Will），尚未上線；夜間的 `HALTED/auto` 會停到人醒來。
- `external` 已成交借款不可召回；包絡（D1）界定每筆新單的最壞條款：可用資金以下限利率借最長天期。
- `inherited` resume 要 TOTP：仍成立，理由是控制面經 Funnel 公開；自動恢復不經公開控制面，不受此限。
- `inherited` 狀態規則的權威在 PostgreSQL trigger、Python 只做較早的錯誤（parity 測試）：仍成立，
  bot 與 webapi 分屬不同 role，規則放 DB 才能約束所有寫入者。
- `inherited` 重述 HALTED 不寫新列（`restates()`）：原本無害，自動恢復後會讓「HALTED/auto 期間按的 kill」
  被當成重述而跟著自動解除，**不再成立**。

## Options Considered

- **基準. 時間到期的鎖**：Freqtrade Protections 觸發後鎖 `stop_duration`，到期自動解除，不看原因是否消失。
  來源：https://www.freqtrade.io/en/stable/plugins/ 。
- **A. 維持人工恢復**（D4 原狀）。
- **B. 條件式恢復＋次數上限（採用）**：停機滿最短時間、之後連續 N 個被接受的 snapshot 都沒有再觸發第 3 級，
  且滾動 24 小時內自動恢復次數未達上限，才寫 `ACTIVE/auto`。

## Decision（2026-09-26 Will 拍板 B 與預設參數）

- **D1 條件**：`HALTED/auto` 滿 **15 分鐘**，且停機後**連續 3 個被接受的 snapshot** 沒有任何第 3 級 trip；
  其間任何 trip（含已停機時的持續觸發）讓計數歸零。
- **D2 上限**：滾動 **24 小時內最多自動恢復 2 次**；第 3 次停機維持到 operator resume，並告警已達上限。
- **D3 operator 優先**：operator 的 HALTED 一律寫新列（蓋過 `HALTED/auto`），`HALTED/operator` 永不自動解除。
- **D4 權威在 DB**：trigger 只多允許 `HALTED/auto` → `ACTIVE/auto`，以 DB 時鐘檢查 15 分鐘與 24 小時
  次數，並替所有 auto 列蓋 DB 時間；snapshot 是否乾淨由 bot 判斷，證據（trip 原因、停機時長、snapshot
  序號）寫進 reason，經既有 trading-state 告警送 Telegram。
- **D5 兩種 trigger 是「不再發生」而非「條件消失」（2026-09-26 design review 後 Will 選 A）**：
  `venue_lent_above_ledger` 比相鄰 snapshot 的變化量，觸發後基準重設；`command_rate_exceeded` 只在送單
  被拒時觸發，停機期間不送單。兩者停機後都無法重新觀測，實質是「15 分鐘、3 個 snapshot 內未再發生」
  即恢復，靠 D2 上限擋住反覆發生的 bug。

## Rationale

- **選 B 而非基準**：第 3 級代表 bot 對自己 offer 的認知與 venue 不一致；時間到就解除會在錯帳上繼續下單。
  衝突類（durable 紀錄與證據互相矛盾、重試解不開）會一直被偵測到而自然維持停機；lent 與節流（D5）
  則是「未再發生」。**D5 不改成絕對值比對或人工恢復**：多出的借出已成交、停機收不回，要防的是 bug
  持續——持續就會再觸發、一天第 3 次停住；節流停機時條件確實已消失。絕對值比對要重寫敏感的偵測器，
  人工恢復則讓最常見的時序誤判仍要人半夜處理。
- **選 B 而非 A**：包絡已界定每筆單的最壞條款，恢復後的單仍在包絡內；短暫錯誤讓資金停到人醒的閒置成本，
  高於在條件已消失時恢復的風險。
- **上限的理由**：3 個 snapshot 抓不到的間歇性 bug 會停停走走；2 次／日後停住，把它交回人。
- **代價**：(1) 間歇性 bug 每日最多多跑兩輪才停住；(2) 計數在記憶體，重啟歸零，只會更晚恢復（偏保守）；
  (3) 「乾淨」的判斷在 bot 端，DB 只擋時間與次數，bot 判斷錯誤時 trigger 擋不住提前恢復的內容，只擋頻率。

## Expected Outcome

- 時序類第 3 級事件無人介入即恢復；衝突類與 operator kill 維持停機到人處理。
- 同一個 bug 一天最多造成兩次自動恢復，第三次停住並告警。

## Followup

- ✅ 實作：`bf2c27a`（PR #21 merge `692484b`），migration `8e4b2f6a1c37`；trigger 以 SQLSTATE `BX001`／`BX002`／`BX003` 拒絕（非法轉移／未滿 15 分鐘／24h 已達 2 次），Python `validate_transition` 鏡像規則並由 parity 測試釘住；trip 與 clean 觀測共用一個 inbox 依到達順序處理，同批中 trip 優先。plan：`2026-09-26-auto-resume.md`（原文已不在 repo，本 ADR 即紀錄）。
- 未做 live 驗證：production 未刻意誘發第 3 級 trip 觀察自動恢復，行為僅由 unit／integration／parity 測試涵蓋；首次真實自動恢復時核對 Telegram 告警與 reason 內容。

## Invariants

- `HALTED/operator` 只有 operator resume 能結束；自動恢復只作用於 `HALTED/auto`。
- 自動恢復前至少 15 分鐘、3 個乾淨且被接受的 snapshot；24 小時內不超過 2 次，DB trigger 強制時間與次數。

## Revocation Triggers

- 出現自動恢復後造成可歸因於 bot 的損失 → 收緊條件或回到 A。
- Phase 5 多租戶 → 上限與計數改為每 tenant，並重評是否要持久化計數。

## Related

- 部分取代：[2026-09-25-lending-envelope-replaces-probation-and-account-halt](2026-09-25-lending-envelope-replaces-probation-and-account-halt.md) D4 與其 Invariant「第 3 級停機不自動解除」。
- 來源：2026-09-26 與 Will 的對話（本 ADR 即原始紀錄）。程式碼事實取自 bfx-funding-bot main `d9cf002`：
  `backend/src/bfx_funding_bot/modules/execution/safety/protection.py`、`safety/trading_state.py`、
  `boot_recovery.py`（`_protect`）、`backend/alembic/versions/5b1e7c9d2a40_two_state_trading_control.py`。
