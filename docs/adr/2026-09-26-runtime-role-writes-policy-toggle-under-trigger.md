---
title: 幣別開關由 bot runtime role 寫入新 policy revision，以 DB trigger 綁定到仍有效的 operator 請求
date: 2026-09-26
status: active
tags: [bfx-funding-bot, decision, security, database, operator-request]
---

# bot runtime role 以 trigger 限定寫入幣別開關

## Context

prior state：migration `a9d3e5f7b102`（capital authority）對 `bfx_bot` 撤銷 `capital_policy_revisions`／`capital_policy_heads`
的所有寫權，policy 只能由 owner role 以 `scripts/amend_capital_policy.py`（dry run → digest → apply）在 VM 上 SSH 改。
[條款包絡 ADR](2026-09-25-lending-envelope-replaces-probation-and-account-halt.md) D4 把每幣別 `enabled` 定為日常停止方式，
2026-09-26 design review 後 Will 拍板改走 TOTP UI；於是得決定「誰把 UI 請求寫成新的 policy revision」。

**約束**：

- `external` 控制面經 Tailscale Funnel 對外公開；任何放大交易的動作都要 operator 的 TOTP session。
- `inherited` web API 對 ledger／projection／policy 零寫權，只 INSERT operator request（ADR D4' outbox）：仍成立，
  webapi 是對外面，縮小它的寫權是這個系統的安全邊界。
- `inherited` 「bot 不可寫 policy」（`a9d3e5f7b102`）：當時 policy 只有人改，runtime 沒有寫的理由；幣別開關出現後這個前提不再成立。
- `inherited` 權限一律寫在 migration、以真實 role 測試（operator request outbox 的開發慣例）：仍成立。

## Options Considered

- **基準. operator request outbox**：web 層只記請求，單一 writer 在交易邊界內重驗權限後套用（本 repo 的 resume/kill、
  uncertainty 裁決已採用；transactional outbox，<https://microservices.io/patterns/data/transactional-outbox.html>）。
  需要套用者有寫權，所以落到下面的 A。
- **A. daemon（bot role）套用，DB trigger 限定寫入範圍（採用）**：bot 取得 revisions 的 INSERT 與 heads 的
  `UPDATE(revision_id, revision)`；非 owner 的寫入由 trigger 限定：只能是 head 的下一號、除 `enabled` 外內容完全相同、
  `source.request_id` 指向仍在等待、同 scope／幣別／方向且 `operator_authorized` 仍通過的請求；head 只能前進一格。
- **B. webapi 直接寫 policy**：少一跳，但把金流設定的寫權放到對外面，違反 D4'。
- **C. 維持 owner-only，operator 繼續 SSH 跑 amend 腳本**：權限最小，但日常停止要最高權限與 VM 存取，緊急 kill 反而在 UI 上。

## Decision

- **D1** 採 A：幣別開關走新 outbox `capital_policy_requests`（migration `7d2a9c4e6b13`），`CapitalPolicyRequestWorker`
  在 account lock 下重驗 operator、經 amendment 路徑寫新 revision；owner role 與 amend 腳本不受 trigger 限制，仍是改包絡數值的途徑。
- **D2** kill 把等待中的 enable 標成 `superseded_by_kill`，等待中的 disable 照常套用（只縮小交易範圍）。

## Rationale

- **A 而非 B**：寫權留在不對外的 daemon，webapi 仍只能 INSERT 請求欄位；代價是 runtime role 從「零寫 policy」變成「有條件寫」。
- **A 而非 C**：日常停止不該需要最高權限；trigger 讓 bot 就算被攻破或有 bug，也只能照一筆真實、仍授權的請求翻轉 `enabled`，
  改不了包絡、金額上限或其他幣別。代價：多一組需要與程式同步的 trigger，靠 `test_capital_policy_request_roles.py` 以真實 role 驗證。
- **D2**：enable 會在 resume 後擴大交易，停機前送出的擴大請求應重新決定；disable 在任何狀態都安全。

## Expected Outcome

- operator 日常停用／啟用幣別只經 UI＋TOTP；SSH 只剩改包絡數值。
- 以 `bfx_bot` role 偽造 request id、改包絡或跳號寫 head，都被 DB 拒絕（有真實 role 測試）。

## Invariants

- webapi 對 policy 表只有 SELECT；bot 對 policy 的寫入必須綁定仍等待且 `operator_authorized` 的請求，且只改 `enabled`。

## Revocation Triggers

- Phase 5 讓 tenant 改包絡數值 → trigger 的「只改 enabled」要擴充，重評是否改由專用 SECURITY DEFINER 函式寫入。
- 出現第二種 runtime 需要寫 policy 的情境 → 重評，不要在 trigger 上疊例外。

## Related

- 來源：2026-09-26 與 Will 討論（design review 後的「加 UI 開關」決定），本 ADR 即原始紀錄；實作 PR #19（merge `2f3c70f`），
  commit `e206649`（outbox 與 worker）、`2629734`（trigger 綁定真實請求）。
- 上層：[2026-09-25-lending-envelope-replaces-probation-and-account-halt](2026-09-25-lending-envelope-replaces-probation-and-account-halt.md) D4 與 Amendment。
- TOTP 前提（operator 登錄與首位 admin 指派）：[2026-09-20-operator-totp-enrollment-and-host-only-admin-bootstrap](2026-09-20-operator-totp-enrollment-and-host-only-admin-bootstrap.md)。

## Amendment (2026-10-08): G1 改以「請求無 outcome」判斷仍在等待

D1 trigger（`guard_runtime_policy_revision()`）的「`source.request_id` 指向仍在等待的請求」，在 [2026-10-08-operator-requests-insert-only-with-insert-once-outcome](2026-10-08-operator-requests-insert-only-with-insert-once-outcome.md) 的 R1 起改為「該請求沒有 outcome 列」（原為 `state='requested'`）；bot 須在同一交易內先寫 revision、再寫 outcome。其餘條件（同 scope／幣別／方向、`operator_authorized`、只改 `enabled`、head 只前進一格）不變。D2 的 supersede 改成對無 outcome 的等待中 enable 寫 `rejected`／`superseded_by_kill` outcome。
