---
title: 已用盡的 canary epoch 由 operator 明示續開，不自動釋放、不經降級
date: 2026-09-23
status: active
amends: "[2026-09-22-maintenance-halt-without-canary](2026-09-22-maintenance-halt-without-canary.md)"
tags: [bfx-funding-bot, decision, safety, halt, canary, permit]
---

# 已用盡的 canary epoch 由 operator 明示續開

## Context

canary permit 綁 halt epoch 且 `UNIQUE(halt_id)`，在**下指令當下**即 consumed——這是對的：結果不明的送單
絕不能自動重試（[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md) D4）。2026-09-22 07:49 的 canary 被
Bitfinex 回 HTTP 500，事後交易所查詢證明什麼都沒掛上（`zero_match` 裁決），halt 9 的 epoch 卻已花掉。

接著四個各自正確的守衛組成完全沒有出路的狀態：halt 9 是 `safety` → `resume()` 拒絕；唯一出口是 promote →
需要 canary；permit 已 consumed → `UNIQUE(halt_id)` 擋住；再 halt 一次 → `set_halted` 在已 halted 時直接
`return current`，拿不到新 epoch（docstring：「Reasserting a halt retains its authorization epoch」）。

## Options Considered

- **A. operator 明示續開（採用）**：`set_halted(..., renew=True)`，只經 `POST /admin/halt?renew=true`；
  已 halted 時仍 append 一筆新 row，**kind 沿用被續開的那筆**。
- **B. `zero_match` 裁決後自動釋放 permit**：不需人工；把 permit 表與 uncertainty 表耦合，並在不明拒因下自動再送真單。
- **C. 把 halt 9 的 kind 改成 maintenance → resume → 等 worker 重新下 safety halt 取得新 epoch**：零 code 變更。

## Decision

- **D1**：採 A。續開是一筆有 reason、有 actor 的稽核 row，語意是「那次嘗試什麼都沒買到，我授權再試一次」。
- **D2**：kind 取自當前 halt 而非呼叫者——端點預設 kind 是 `maintenance`，若採呼叫者的值，續開就成了把
  safety halt 降級成可 `resume()` 的後門。`resumable_without_release` 因此恆為 false。
- **D3**：一般再 halt 仍是 no-op（守衛持續 reassert，必須零成本）；`renew` 預設 false。

## Rationale

- **選 A 而非 B**：兩次失敗時我們**都不知道交易所為什麼拒絕**（5xx body 被丟棄，見
  [2026-09-23-venue-refusal-kept-before-classified](2026-09-23-venue-refusal-kept-before-classified.md)）。拒絕的理由本身可能就是不該再試的理由；B 會在當天多送
  兩次真單而沒人看著。代價：每次重試要 operator 按一次。
- **選 A 而非 C**：C 在 resume 到 worker 重新 halt 之間有空窗，reconciler 可能在那時以正常額度送單——
  2026-09-22 早上 5 分鐘停機就發生在同一個空窗。C 還會在稽核紀錄上留下一筆「維護性暫停」的假敘事。
- **為何 kind 沿用**：續開改變的是「能再試幾次」，不是「為什麼停」。

## Expected Outcome

- 已用盡的 epoch 有一條可稽核、不降級的出口；production 已用兩次（halt 10、halt 11），皆為 `safety` /
  `resumable_without_release=false`。
- 一般 reassert 仍不產生新 row。

## Revocation Triggers

- 交易所拒因可被結構化分類並判為 durable rejection（D2 落地）後，重評 B 是否在「明確被拒」的子集上可自動化。
- 出現 operator 以 renew 反覆重送而未處理拒因的紀錄 → 加上重試次數或冷卻期限制。

## Lessons

### Rules

- **R1**：本週第四次同型事故——每個守衛單看正確、組合起來鎖死（halt epoch 綁定、金額嚴格相等、quote 綁整點、
  permit 綁 epoch）。單元測試逐一驗證守衛，不會發現組合沒有出口。`Rule: 新增或修改一個 fail-closed 守衛時，
  列出「系統停在這個守衛上之後，恢復需要經過哪些其他守衛」，並在測試或 runbook 裡走通一次完整的恢復路徑；
  走不通就是死結，不是安全。`

## Related

- 修訂 [2026-09-22-maintenance-halt-without-canary](2026-09-22-maintenance-halt-without-canary.md)（halt kind 與 resume 的邊界）；permit 綁 epoch 的原始規定見
  [2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md) D4
- 實作 commit `1e4f7dc`；測試 `tests/modules/execution/test_halt_epoch_renewal.py`（含「不得降級」）
- 本 ADR 即原始紀錄，無外部來源文件
- Lessons R1 對應的通用教訓：fail-closed guard 組合死鎖（本案為其中一例）
- 2026-09-25 採用 [2026-09-25-automated-probation-replaces-release-ceremony](2026-09-25-automated-probation-replaces-release-ceremony.md)：canary permit 與 epoch 續開由變更分級＋受限額度期取代。
