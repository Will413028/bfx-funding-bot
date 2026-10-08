---
title: migration 往後只往前走：新 migration 的 downgrade 預設拒絕，升級前狀態的 fixture 改成「升到上一版再 seed」
date: 2026-10-08
status: active
tags: [bfx-funding-bot, decision, database, migration, testing]
---

# migration 往後只往前走

## Context

正式環境早就不 downgrade：[2026-05-31-koyeb-to-vm-single-writer-lock](2026-05-31-koyeb-to-vm-single-writer-lock.md) D2 規定永不自動 `alembic downgrade`、回滾靠還原 DB；`docs/runbooks/deploy.md`「migration 之後失敗」只允許 roll forward，或把備份還原到隔離庫比對。但 migration 的寫法沒跟上：2026-10-08 時 76 支 migration 都有 `downgrade()`，其中 1 支無條件 raise（`9b2c3d4e5f6a`）、10 支在有資料時拒絕、1 支是 `pass`（baseline `a376b830a8f1`），其餘是可執行的反向 DDL。#136 的 `f5a6b7c8d9e0` 為了讓 downgrade「精確還原」舊 CHECK 與 guard，多寫了一份反向邏輯，design review 把「要不要繼續這樣寫」交給 Will。

實際呼叫 downgrade 的只有測試：26 個測試檔，部分拿它造「升級前」的狀態（例如 `tests/integration/test_ledger_json_sql_null.py` 的 `before` fixture 在 head seed 後 downgrade 到上一版），`test_webapi_privilege_allowlist.py` 要求每一步 downgrade 都精確還原權限清單。

**約束**：

- `external` Bitfinex 已經發生的寫入（送單、成交、撤單）無法撤回，append-only ledger 的事實也不可刪；schema 退回舊版不會讓 venue 狀態跟著退回（`docs/runbooks/rollback-after-venue-write.md`）。
- `inherited` bfx-deploy 在 migration 期間只停 bot、webapi 繼續跑上一版 image，migration 後不 downgrade（[2026-09-25-ci-registry-digest-deploy](2026-09-25-ci-registry-digest-deploy.md)）：仍成立，是部署工具的現行行為；它要求 schema 變更向前相容（expand／contract），與 downgrade 無關。
- `inherited` 既有 76 支 migration 與 26 個測試檔依賴現有 downgrade：仍成立但屬自我約束（一致性與工作量），不是外部限制。

## Options Considered

- **基準. 正式環境只往前走（forward-only）**：Flyway 的 undo 只在付費的 Teams 版提供，文件指出它救不了中途失敗的 migration 或破壞性變更，建議改為「DB 與正式環境上所有版本的程式向後相容，加上經過測試的備份還原」（[Flyway: Undo migrations](https://documentation.red-gate.com/flyway/flyway-concepts/migrations/undo-migrations)「Important notes」）；Stripe 的大型線上遷移走四步雙寫（雙寫、改讀、改寫、刪舊），每一步都只往前（[Stripe: Online migrations at scale](https://stripe.com/blog/online-migrations)）。migration 的 downgrade 只是開發便利，不是回滾手段。
- **A. 維持可逆慣例，用 ADR 寫明 downgrade 只給測試與開發用**：Rails／Django 的預設（可逆，例外用 `ActiveRecord::IrreversibleMigration`）。GitLab 要求每支可逆，理由是自架環境遇到漏洞或 bug 時要能降版、開發者切換分支時 schema 要一致；但 GitLab.com 正式環境出問題時用 roll-forward，不跑 `db:rollback`（[GitLab: Migration Style Guide](https://docs.gitlab.com/development/migration_style_guide/)「Reversibility」）。
- **B. 往後只往前走**：新 migration 的 `downgrade()` 預設 raise；無損且便宜的反向（例如只換 function 本體）作者仍可寫。既有 migration 不改；需要升級前狀態的測試改用「升到上一版 → seed → 升到 head」。
- **C. B 再回頭刪掉既有 76 支的 downgrade**：一次統一。
- **D. 只在 fixture 需要時才寫 downgrade**：沒有明確規則。

## Decision

- **D1**：採 B。新 migration 的 `downgrade()` 預設 `raise NotImplementedError("forward-only: docs/adr/2026-10-08-forward-only-migrations.md")`；只有反向無損（不丟資料、不丟權限語意）且不需要額外測試時才寫。
- **D2**：需要升級前狀態的測試改用 `tests/pg_templates.py` 的 helper：在 template 上 `upgrade <parent>`、seed、再 `upgrade head`。既有用 downgrade 造狀態的 fixture 不強制改寫，碰到時再換。
- **D3**：既有 76 支 migration 與 `test_webapi_privilege_allowlist.py` 的逐步 downgrade 檢查維持原樣。

## Rationale

- **D1**：正式環境的回滾路徑已經是 roll forward 或還原隔離庫，downgrade 程式碼服務的是一條 prod 不走的路，還要測試去維持它（`f5a6b7c8d9e0` 的精確還原就是例子）。選 B 而非 A：A 讓每支 migration 繼續付出反向邏輯與測試的代價，換來的只是開發便利；GitLab 需要可逆是為了自架環境降版與開發切分支，我們是單一部署、沒有降版需求，而 GitLab 自己的正式環境也走 roll-forward。保留「無損時可寫」，是因為換 function 本體這類反向幾乎零成本，禁止反而多一條規則要記。
- **D2**：用 downgrade 造升級前狀態，等於用一條不受信任的路徑當測試前提；在上一版 schema 上直接 seed，前提就是真實的升級起點，和正式環境的升級順序一致。代價是 helper 要能把 template 停在指定 revision。
- **D3**：不選 C：改 76 支 migration 與 26 個測試檔是白工，舊 downgrade 不會造成錯誤，只是不再被要求；等 chain squash 時自然消失。不選 D：「fixture 需要才寫」在 review 時無法判斷該不該有，規則等於沒有。

## Expected Outcome

- 新 migration 不再出現「精確還原」的反向邏輯與對應測試；review 時看到可執行的 downgrade，要能說出它為何無損。
- 新的升級前狀態測試都經由 D2 的 helper，不呼叫 `alembic downgrade`。

## Followup

- 已完成：`tests/pg_templates.py` 的 `template_at(revision, prepare=None)` 建「空庫升到指定 revision 並 stamp `ci`」的 template build；常數 `LAST_REVERSIBLE_REVISION` 見下方 Amendment。下一支需要升級前狀態的 migration 測試（第一個使用者是 operator request outbox 重構的 R1）用它。
- `backend/ARCHITECTURE.md` 的 migration 段落加一句指向本 ADR。

## Revocation Triggers

- 出現需要降版的部署形態（多租戶自架、客戶端管理的版本）→ 重評 A。
- 做 chain squash（新 baseline）→ 舊 downgrade 隨之移除，D3 結案。

## Review Notes

## Amendment (2026-10-08): 從 head 降版的測試改從最後可逆 revision 起降

- D3 原寫「既有逐步 downgrade 檢查維持原樣」；實際上只要 head 有一支 forward-only migration，所有「先升到 head 再 downgrade」的測試都會在它的 downgrade 失敗（在 head 加一支 downgrade 會 raise 的探針實測：22 檔約 110 個測試）。
- 改為（Will 2026-10-08）：這些測試改從 `tests/pg_templates.py` 的 `LAST_REVERSIBLE_REVISION`（`8ac3b44460fc`，forward-only 之前最後一支 migration）起降，保留原本的 downgrade 斷言；斷言 head 狀態的測試（權限白名單、`alembic check`、realm trigger 涵蓋）仍跑在 head，round trip 的 `alembic check` 移到升回 head 之後。
- 不採「改成在上一版 seed 再升級」：斷言意圖會變，例如權限白名單不再驗證逐步 downgrade 的還原。不採「新 migration 例外寫可執行的 downgrade」：違反 D1。（此處的取捨與上方 Options 的 A–D 無關。）
- 後果：這些測試只涵蓋 forward-only 之前的 migration；之後的 migration 的升級前狀態測試照 D2。
- 權限白名單：`LAST_REVERSIBLE_REVISION` 的 public 白名單寫成 `REVERSIBLE_*`，較舊版本的表權限從它推導；之後改 public 表或欄位權限的 migration 只改 head 的 `EXPECTED_*`，改 `legacy_archive` 或 schema 權限的要先把那部分同樣拆成兩份（`tests/integration/test_webapi_privilege_allowlist.py`）。

## Related

- 本 ADR 即原始紀錄：2026-10-08 Will 在 coding agent 對話中選定 B；起因是 #136（`f5a6b7c8d9e0`）design review 交付的決定。無其他外部來源文件。
- [2026-05-31-koyeb-to-vm-single-writer-lock](2026-05-31-koyeb-to-vm-single-writer-lock.md) D2（永不自動 downgrade）、[2026-10-06-switch-by-seed-and-retire-the-legacy-authority](2026-10-06-switch-by-seed-and-retire-the-legacy-authority.md)（migration 後不 downgrade 的部署約束）。
- `docs/runbooks/deploy.md`「migration 之後失敗」、`docs/runbooks/rollback-after-venue-write.md`。
