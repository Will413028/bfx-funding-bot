---
title: operator 請求只准新增，處理結果另寫 insert-once outcome 表；single-pending 改由 BEFORE INSERT trigger 保證
date: 2026-10-08
review-date: 2027-01-08
status: "superseded-by: [2026-10-08-operator-requests-keep-state-effects-carry-request-id](2026-10-08-operator-requests-keep-state-effects-carry-request-id.md)"
tags: [bfx-funding-bot, decision, database, operator-request, security]
---

# operator 請求只准新增，結果另寫 insert-once outcome

> 2026-10-08 由 [2026-10-08-operator-requests-keep-state-effects-carry-request-id](2026-10-08-operator-requests-keep-state-effects-carry-request-id.md)（D8）取代：D1、D2、D4（含 Amendment）、D5、D6 不再生效；保留的部分見該檔 Decision 末項。

## Context

prior state：三張 operator request 表（`trading_control_requests`、`capital_policy_requests`、`uncertainty_resolution_requests`）是 [2026-08-31 D4'](2026-08-31-account-isolated-execution-target-architecture.md) 的 outbox：webapi 只 INSERT 請求欄位，bot worker 在 account lock 下套用後 UPDATE 同一列的 WORKER 欄位（`state`、`processed_at_ms`、`outcome_reason`、產物 FK）。請求欄位不可變靠 BEFORE UPDATE guard 逐欄比對 `REQUEST_COLUMNS`（例如 `5b1e7c9d2a40` 的 `guard_trading_control_request()`）；「每個對象至多一筆待處理」靠 `WHERE state='requested'` 的 partial unique index。[2026-09-28 D3'](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md) 把 ledger 的 attempt／outcome／resolution 改成 insert-only，但 outbox 明文「維持現有機制」。2026-10-08 盤點（快照 `d8787ac5`）看到這個機制的代價：guard 要跟著欄位清單維護、三表的產物連結方向不一致（trading、capital 是請求 → 產物，capital 另有 JSON 反向連結，uncertainty 是 journal → 請求）、`insert_request` 把所有 IntegrityError 當成 pending（空白 reason 回 409 `request_pending`）。

**約束**：

- `external` 正式環境在線、真錢放貸中（ledger authority 2026-10-05 切換）；改動要能在不中斷 operator 控制的情況下分版上線。
- `inherited` webapi 對執行狀態零寫權、只 INSERT 請求（D4'）：仍成立，webapi 是對外面，這是安全邊界。
- `inherited` 每帳戶單一 writer，在 account advisory xact lock 下處理請求（[2026-05-31-koyeb-to-vm-single-writer-lock](2026-05-31-koyeb-to-vm-single-writer-lock.md) D1）：仍成立，是 bot 的執行模型。
- `inherited` bfx-deploy 跑 migration 時只停 bot、webapi 仍跑上一版 image；bot 在 schema head 不符時拒絕開機（`core/schema_head.py`），所以回滾只能 roll forward：仍成立，是部署工具現行行為。新 migration 不寫可執行的 downgrade（[2026-10-08-forward-only-migrations](2026-10-08-forward-only-migrations.md)）。
- `external` table owner 可停用 trigger、跑 migration 的也是 owner；任何設計都只防得住 runtime role（PostgreSQL 權限模型）。

## Options Considered

- **基準. 請求不可變，結果是另一筆只寫一次的紀錄**：FIX 的 NewOrderSingle 不被改寫，結果以獨立的 Execution Report 回報（[FIX 4.4 MsgType 8](https://www.onixs.biz/fix-dictionary/4.4/msgtype_8_8.html)「confirm the receipt of an order」「reject orders」）；TigerBeetle 的兩階段轉帳不修改 pending transfer，而是另建一筆 post／void transfer，且「A pending transfer can only be posted or voided once」（[TigerBeetle: Two-Phase Transfers](https://docs.tigerbeetle.com/coding/two-phase-transfers/)「All Transfers Are Immutable」）。本 repo 的 ledger D3' 也是同型。即下面的 B。
- **A. 保留單表，guard 改成白名單**：guard 改比對 `to_jsonb(NEW) - <WORKER 欄位>` 與 `to_jsonb(OLD) - <WORKER 欄位>`，新增的請求欄位自動受保護；一支 migration。bot 仍有 UPDATE 權，WORKER 欄位清單仍寫在 trigger 參數裡。
- **B. 請求只准新增＋每表一張 insert-once outcome 表**：outcome PK＝`request_id`，任何 role 不得 UPDATE／DELETE／TRUNCATE；請求是否待處理由「有沒有 outcome」推導；請求表最後拒絕所有 UPDATE，不列任何欄位。需四個 release（expand、bot 改寫 outcome、撤 UPDATE、DROP 舊欄位），single-pending 不能再用 partial unique index。
- **C. 三張請求表併成一張通用 `operator_requests`＋一張 outcome**：結構最少；但三種請求的 payload、scope、授權規則不同（capital 有 symbol／action 與 G1 授權，uncertainty 綁 observation），要用 JSON 或稀疏欄位，CHECK 與 FK 失去型別。

決策前另取兩份獨立草稿（未告知傾向）。草稿 1 主張 A：請求只有「待處理 → 終態」兩態，資金後果已經記在 insert-only 的效果表（`trading_state`、policy revision、resolution journal），請求列本身不是資金事實；partial unique index 是宣告式、沒有 race；並指出逐欄列舉的 guard 一旦引用不存在的欄位，worker 的 UPDATE 會失敗，kill 交易裡的 HALTED 也跟著 rollback。草稿 2 主張 B，另提 typed FK、client 產生的 `request_id`、failed 終態不重試；也自承 trigger＋lock 比 index 脆弱，雙向連結可能出現兩份真相。

## Decision

編號沿用 2026-10-08 拍板時的題號：D3（測試如何從 head 降版）記在 forward-only ADR 的 Amendment，D7（prod 唯讀查詢方式）屬執行程序，不在本檔。

- **D1**：採 B。outcome 一律普通 INSERT，衝突即錯，不用 `ON CONFLICT DO NOTHING`（與 D3'「衝突重複不當冪等」一致）。
- **D2 產物連結**：outcome 帶產物 FK（trading `trading_state_id`、capital `policy_revision_id`）；uncertainty 維持 journal → 請求（`execution_resolution_journal.operator_request_id`）。capital 的 `source->>'request_id'` 保留為 G1 的授權鍵。
- **D4 single-pending**：保留語意（trading 的 kill 與非 kill 各一格），改成 BEFORE INSERT trigger：先取對象的 `pg_advisory_xact_lock(hashtext('bfx_operator_request:<table>'), hashtext(<對象 key>))`，再檢查「無 outcome 的同對象請求」，違反時 `RAISE ... USING ERRCODE='unique_violation', CONSTRAINT='<既有 index 名>'`。`insert_request` 只把 23505 且 constraint 名屬於該表 pending 名集合者當成 pending，其餘 IntegrityError 往上拋。
- **D5 outcome 的 CHECK**：依表沿用現行規則：uncertainty applied 的 reason 必須 NULL，其餘不限；trading applied 必須帶 `trading_state_id`（現行 CHECK 允許 NULL，但正式程式路徑一定會設）。
- **D6 scope**：outcome 帶 `exchange_account_id`、`deployment_environment`，以複合 FK `(request_id, exchange_account_id, deployment_environment)` 指向請求（請求表加對應 UNIQUE），掛 `database_realm_write`。
- 不做：client 產生的 `request_id`／idempotency key（保留 single-pending 就足以擋重送）。
- `failed` 維持終態、worker 不重試（現行行為，草稿 2 亦同）；要重試由 operator 送新請求。

## Rationale

- **D1 B 而非 A**：A 回答了「新欄位會不會漏保護」，但沒回答「誰能改請求列」：bot 仍有 UPDATE 權，請求列的終態仍是可覆寫的欄位，白名單只是換一份要維護的清單。B 讓 PostgreSQL 的權限與 PK 直接保證「請求寫入後不變、至多結案一次」，guard 不再引用任何欄位，草稿 1 指出的「guard 引用不存在欄位讓 kill rollback」也隨之消失。代價是四個 release、六張表的權限與 realm 測試，以及 single-pending 從宣告式 index 變成 trigger＋lock。草稿 1「資金後果已在效果表」成立，所以這不是資金正確性的修補，而是把 outbox 拉到與 ledger D3' 同一個不可變模型。
- **D1 不選 C**：三種請求的授權與 scope 規則不同，併表會把型別化的 CHECK／FK 換成 JSON，G1 這類跨表 guard 要先解 JSON；目前只有三種請求，重複的結構成本低於失去型別的成本。
- **D2 A 而非「效果表帶 `operator_request_id`」**：outcome 帶 FK 時 backfill 直接搬舊 WORKER 欄位，不必在 migration 裡暫停 append-only 效果表的保護；「unchanged」這類沒有新效果的 applied 在效果 → 請求方向下沒有連結可寫。代價是 capital 仍有兩個方向的連結（outcome → revision、revision.source → 請求），兩者只在 G1 插入當下交叉驗證，這正是草稿 2 說的「兩份真相」風險，接受它是因為 `source` 是 G1 的授權鍵，拿掉要改 trigger 合約。
- **D4 trigger 而非 index**：B 之後請求表沒有 `state`，partial unique index 沒有可用的條件。草稿 2 指出 trigger＋lock 比 index 脆弱；所以並行行為要有測試（兩個 connection 同時 INSERT 同對象，只成功一筆），拿掉 lock 時要失敗。沿用既有 constraint 名，讓 API 的 409 判斷不必跟著改。
- **D5**：統一成「applied 一律帶 reason、帶產物」需要改寫 uncertainty 歷史列才能 backfill；規則差異是既有行為，前端已容忍。
- **D6**：與現有每張 realm 表一致，outcome 可以不 join 請求就依帳戶與 realm 過濾，realm guard 不必另寫跨表版本；代價是請求表多一個 UNIQUE。
- **不做 idempotency key**：FE 重送目前由 single-pending 擋下（第二筆回 409），尚未觀察到重送造成的錯誤。

## Expected Outcome

- R3 之後 `bfx_bot` 與 owner 對三張請求表的 UPDATE 都被拒；`has_column_privilege('bfx_bot', <請求表>, <欄>, 'UPDATE')` 全部 false。
- 同一請求第二筆 outcome 被 PK 拒絕；已有 outcome 的請求不會被 worker 重撿。
- 空白 reason 回 422，不再回 409 `request_pending`。
- 狀態一律由 outcome 推導：`backend/src` 不再出現 `state='requested'` 的條件。

## Followup

- 實作分四個 release（R1 expand、R2 bot 直寫 outcome、R3 撤 UPDATE、R4 DROP 舊欄位），每個 release 部署成功後才 merge 下一個；計畫與盤點在本機 phase plan（gitignored）。
- R1 migration 依 [2026-10-08-migrations-assert-their-data-and-deploy-rehearses-them](2026-10-08-migrations-assert-their-data-and-deploy-rehearses-them.md) 自帶前置斷言（`requested` 卻已有產物、applied 缺產物連結、scope 不符、終態缺 `processed_at_ms`）與後置斷言（終態列與 outcome 逐欄相等），在部署前的演練中先觸發；違規的歷史列由前置斷言擋下。
- 收尾時更新 `backend/ARCHITECTURE.md` §7 與 `docs/runbooks/operations.md`。

## Invariants

- 請求表對所有 runtime role 只准 INSERT；outcome 表對任何 role 不得 UPDATE／DELETE／TRUNCATE。
- 每個請求至多一筆 outcome；每個對象同時至多一筆無 outcome 的請求（trading 的 kill 與非 kill 各一格），由 AFTER INSERT constraint trigger 在對象 advisory lock 下檢查，REPEATABLE READ 的 INSERT 被拒（見 Amendment）。
- capital 套用時 revision 先於 outcome 寫入（同一交易），G1 看的是「請求無 outcome」。

## Revocation Triggers

- 觀察到 FE 重送造成重複請求或 409 誤報 → 重評 client `request_id`／idempotency key。
- 出現第四種 operator 請求，或三種請求的授權規則收斂 → 重評 C（併表）。
- journal 出現第二個寫入者 → 補 journal 與請求的範圍比對（盤點缺陷 6）。

## Review Notes

## Amendment (2026-10-08): single-pending 在 AFTER INSERT 檢查，拒絕 REPEATABLE READ

- D4 原寫「BEFORE INSERT trigger」，計畫並把它排在 `database_realm_write`、`ledger_seed_evidence` 之前先擲。R1 design review 指出這和被取代的 partial unique index 時機相反：index 在 row 寫入時才檢查，晚於所有 BEFORE guard、CHECK、NOT NULL；BEFORE 版會把「同對象已有待處理請求」時的內容錯誤（例如空白 reason 違反 `ck_*_evidence`）蓋成 23505，被拒的 INSERT 也先拿了鎖。
- 改為（Will 2026-10-08，「採業界做法、治本」）：`CREATE CONSTRAINT TRIGGER single_pending AFTER INSERT ... NOT DEFERRABLE FOR EACH ROW`，檢查時排除 `NEW.request_id` 本身；鎖、檢查條件、23505 與沿用的 constraint 名都不變。好處來自 AFTER 時機（與 unique index 同一時點，CHECK、NOT NULL、FK 之後）；`CONSTRAINT` 只宣告意圖。PG18 實驗確認：兩個並行 INSERT 只成功一筆；CHECK 與 FK 違反先擲、且不取鎖；asyncpg／psycopg 收到的 SQLSTATE、constraint 名與 index 違反相同。
- 新增：在 REPEATABLE READ 交易裡 INSERT 請求一律拒絕。鎖之後的檢查要看到鎖之前已 commit 的請求：只有 READ COMMITTED（每個語句在鎖之後取新 snapshot）成立。REPEATABLE READ 的 snapshot 早於等鎖，不成立；SERIALIZABLE 只在雙方都是 SERIALIZABLE 時由 SSI 中止其一，持鎖者是 READ COMMITTED 時兩筆都會 commit，也不成立（更正 2026-10-09，見 migration `2e835b6f4c12` 的 review）。原本這只是「呼叫端碰巧都用 READ COMMITTED」的未聲明前提。
- 不採「改名排到最後（`zz_`）」：仍靠名稱字母序，FK 與 CHECK 也照樣排在 BEFORE trigger 之後。不採 pending slot 表（以 UNIQUE 鍵存待處理對象，業界常見、原生 23505、不受隔離等級影響）：結案時要 DELETE slot，等於把 D1 拿掉的可變列換一張表帶回來。

## Related

- 來源：2026-10-08 Will 在 coding agent 對話中選定 B 與 D2–D6，本 ADR 即原始紀錄；兩份獨立草稿的論點已寫進 Options 與 Rationale，草稿本身未保存。
- 修訂 [2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md) D3'（outbox 不再「維持現有機制」）與 [2026-09-26-runtime-role-writes-policy-toggle-under-trigger](2026-09-26-runtime-role-writes-policy-toggle-under-trigger.md) D1 的 G1 描述（「仍在等待」改為「無 outcome」）；兩份都已加 Amendment 指回本檔。
- [2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md) D4'（outbox 本身不變）。
