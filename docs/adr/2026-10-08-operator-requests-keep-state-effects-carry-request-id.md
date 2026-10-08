---
title: operator 請求保留狀態欄，效果列帶 typed operator_request_id 並以含 scope 的複合 FK 指回請求
date: 2026-10-08
review-date: 2027-01-08
status: active
supersedes: "[2026-10-08-operator-requests-insert-only-with-insert-once-outcome](2026-10-08-operator-requests-insert-only-with-insert-once-outcome.md) 的 D1、D2、D4（含 Amendment）、D5、D6"
tags: [bfx-funding-bot, decision, database, operator-request, security]
---

# operator 請求保留狀態欄，效果帶原因 ID

## Context

prior state：同日稍早的 [2026-10-08-operator-requests-insert-only-with-insert-once-outcome](2026-10-08-operator-requests-insert-only-with-insert-once-outcome.md)（下稱「outcome ADR」）選了「請求只准新增＋每表一張 insert-once outcome 表」，R1 已在分支 `feat/r1-operator-request-outcome`（`aaa161db`、`b4e9f8b5`，未 push、未部署）實作完。R1 的 design review 與 code review 發現：outcome 的產物連結（`trading_state_id`、`policy_revision_id`）只是 `REFERENCES … (id)`，journal 對請求也只驗存在，DB 不保證效果與請求同 scope、同對象；舊表同樣沒有。Will 要求不以現行計畫為前提，對照業界做法、治本、不留技術債。

**約束**：

- `external` 正式環境在線、真錢放貸中（ledger authority 2026-10-05 切換）；既有請求與效果列不可遺失或損壞，migration 自帶前後斷言並在部署前演練（[2026-10-08-migrations-assert-their-data-and-deploy-rehearses-them](2026-10-08-migrations-assert-their-data-and-deploy-rehearses-them.md)）。
- `external` prod 歷史筆數與 trading 配對是否有歧義未知；只能交給 migration 的前置斷言判定。
- `inherited` 部署時上一版 webapi 會在新 schema 上跑一段時間，bot 在 schema head 不符時拒絕開機，回滾只能 roll forward（[forward-only](2026-10-08-forward-only-migrations.md)）：仍成立，是部署工具現行行為。
- `inherited` webapi 只 INSERT 請求欄位（[2026-08-31 D4'](2026-08-31-account-isolated-execution-target-architecture.md)）：仍成立，是對外安全邊界。
- `external` table owner 不受 grant 限制、可停用 trigger（PostgreSQL 權限模型）；任何設計都只防得住 runtime role。
- `external` `uncertainty_id` 指向 attempt 或 quarantine 兩張表（`operator_reads.py` 的 `get_uncertainty`），不能直接寫 FK。
- `external` auto halt、`/admin/halt` 與 owner 腳本寫的效果列沒有對應請求，原因欄必須可為 NULL。

## Options Considered

- **基準. 結果是請求列上只有 worker 能寫的狀態，效果列帶原因 ID，FK 帶 tenant 鍵**：DB job queue 以同一列的 mutable `state` 記結果（[Oban.Job](https://hexdocs.pm/oban/Oban.Job.html)、[River](https://riverqueue.com/docs)）；Kubernetes 以獨立權限的 `status` 區隔誰能寫結果（[API conventions: spec and status](https://github.com/kubernetes/community/blob/master/contributors/devel/sig-architecture/api-conventions.md#spec-and-status)）；效果帶原因 ID 見 event sourcing 的 `causation_id`（[Marten metadata](https://martendb.io/events/metadata.html)）、FIX ExecutionReport 的 `ClOrdID`、[TigerBeetle](https://docs.tigerbeetle.com/coding/two-phase-transfers/) 的 `pending_id`、[brandur 的 idempotency keys](https://brandur.org/idempotency-keys) 的 `rides.idempotency_key_id`；FK 帶 tenant 鍵見 [Citus multi-tenant](https://docs.citusdata.com/en/v9.2/use_cases/multi_tenant.html)。即下面的 E′。
- **A. outcome ADR 原案＋補 scope 檢查**：保留 outcome 表與產物 FK，產物改複合 FK、journal 補複合 FK。改動最小；連結方向仍不一致（trading、capital 是 outcome → 產物，uncertainty 是 journal → 請求），G1 仍讀 `source->>'request_id'` JSON，single-pending 仍是 constraint trigger＋advisory lock＋拒絕 REPEATABLE READ。
- **B. outcome 表只記決定，效果帶原因 ID**：outcome 去掉產物欄，效果側同 E′。「至多結案一次」由 PK 保證；single-pending 仍靠 trigger＋lock。
- **E′. 請求保留狀態欄＋column grant＋不列欄位的終態 trigger＋效果帶原因 ID**：請求列保留 `state`／`outcome_reason`／`processed_at_ms`；bot 只有這幾欄的 UPDATE 權（`5b1e7c9d2a40`、`7d2a9c4e6b13` 已是如此）；trigger 只拒絕「終態再改」與「改回 `requested`」，不列欄位；single-pending 用既有 partial unique index；三張效果表帶 typed `operator_request_id`，以含 scope 的複合 FK 指向請求、partial UNIQUE 保證一個請求至多一個效果；請求表的產物欄退役。
- 「outcome 併入效果＋另設拒絕表」不列為真選項：kill 時已是 HALTED、capital `unchanged` 這類「已套用但沒有變化」沒有效果列，最後仍要一張決定表。

## Decision

- **D8 = 取代 outcome ADR 的 D1、D2、D4（含 Amendment）、D5、D6**：採 E′。
  - 請求欄位不可變由 column grant 保證（runtime role）；不列欄位的 BEFORE UPDATE trigger 拒絕 `OLD.state <> 'requested'` 與 `NEW.state = 'requested'`（含 owner）。
  - 單一 pending 維持四個既有 partial unique index；`insert_request` 以 `INSERT … ON CONFLICT (<該 index 的欄位>) WHERE <其 predicate> DO NOTHING` 判斷 pending 已佔，其餘違規照常拋出（取代 outcome ADR 的 constraint 名比對：那是 trigger 擲合成名稱時才需要的做法）。
  - `trading_state`、`capital_policy_revisions` 加 `operator_request_id uuid NULL`；trading 以 `(operator_request_id, exchange_account_id, deployment_environment)`、capital 再加 `symbol`、journal 以兩條 MATCH SIMPLE 複合 FK（`attempt_id`、`quarantine_id` 各一，`ck_execution_resolution_subject` 保證恰一條生效）指向請求表 `(request_id, …, uncertainty_id)`；以上都指向請求表對應的 UNIQUE；trading、capital 效果表 partial `UNIQUE (operator_request_id)`。
  - G1 改讀 typed `operator_request_id`；`source->>'request_id'` 只留作稽核文字（2026-10-09 Amendment：新 revision 不再寫，見下）。
  - 請求表的 `trading_state_id`、`policy_revision_id` 在效果帶原因 ID 的同一個 release 停寫並 unmap，下一個 release DROP。
- **D9 請求表產物欄不外露、cancel-all 也帶原因 ID**：`trading_state_id`／`policy_revision_id` 從 API 回應與前端型別移除（前端正式程式無人讀），不改由效果側取值；`funding_cancel_all_audit` 加 `operator_request_id`（複合 scope FK，`/admin/halt` 與 auto halt 為 NULL），kill 觸發 cancel-all 時寫入。
- 保留 outcome ADR 的其餘部分：`failed` 為終態不重試、不做 idempotency key、空白 reason 回 422、`insert_request` 其餘 IntegrityError 往上拋。

## Rationale

- **E′ 而非 B**：outcome ADR 選 B 的理由是「bot 仍有 UPDATE 權，請求列終態可覆寫」。前一半早已由 column grant 擋住：bot 只能 UPDATE worker 欄位，新增的請求欄位預設沒有 UPDATE 權；剩下的「終態被覆寫」一支不列欄位的 trigger 就能擋。B 為了 insert-once 付出的代價是 single-pending 從宣告式 index 變成 trigger＋advisory lock＋拒絕 REPEATABLE READ＋合成的 constraint 名，這些正是 outcome ADR Amendment 與兩輪 review 一再修補的地方。insert-once 結果表在業界只出現在跨服務 idempotent consumer 的去重，那裡沒有 single-pending 的需求。代價：「至多結案一次」靠 trigger 而非 PK，且 owner 改請求欄位不再被 trigger 擋（只剩 grant 擋 runtime role）。
- **效果 → 請求而非 outcome → 產物（推翻 D2）**：D2 為了避免在 migration 裡暫停 append-only 保護而選反方向，結果是三表方向不一致、capital 有 typed FK 與 JSON 兩份連結。效果帶原因 ID 才能用複合 FK 一次保證「同 scope、同對象、一個請求至多一個效果」。代價：三張效果表的歷史列要由 owner 在 migration 交易內暫停 append-only trigger 回填，前後斷言檢查；trading 一列可能對到多筆請求，由前置斷言擋下歧義。
- **不選 A**：只補檢查不改方向，G1 的 JSON 授權鍵與 trigger 版 single-pending 都留下來，是 Will 明說不要的技術債。
- **D9 移除而非保留欄位改取效果側**：保留會讓重送 kill 與 capital `unchanged` 的值悄悄變 NULL；移除欄位之後再補是 breaking、新增不是（[Google AIP-180](https://google.aip.dev/180)），所以趁沒有外部使用者時拿掉沒人讀的內部 FK，需要時以新增欄位補連結。重送 kill 不寫新 trading_state，卻在 commit 後另一筆交易重跑 venue cancel-all，所以 cancel-all 的稽核列要自己帶原因 ID，否則 DROP 請求產物欄後這條連結會斷。代價：capital `unchanged` 當時生效的 revision 在 DROP 後推不回來（revision 無時間欄），`unchanged` 的 reason 已表達無變化，接受。
- release 數由四個降為兩個（expand＋停寫＋unmap、DROP），只受相容性窗口約束：上一版 webapi 在窗口內只 SELECT 產物欄，所以 DROP 晚一個 release；這兩欄只有 bot 寫，而舊 bot 在新 schema 上拒絕開機，雙寫期沒有讀者也沒有回滾用途。

## Expected Outcome

- 跨 scope、跨對象的效果列被 FK 拒絕；同一請求第二筆效果被 partial UNIQUE 拒絕。
- 終態請求再 UPDATE 被拒（含 owner）；bot 改請求欄位被 grant 拒絕。
- 兩個 connection 同時 INSERT 同對象請求只成功一筆，由原生 index 擲 23505，不依隔離等級。
- `backend/src` 不再讀 `source->>'request_id'`（G1 起改讀 typed 欄）；migration 只在資料斷言裡讀它，作為與 typed 欄無關的第二份證據（`41cec7caf291` 判斷 capital 請求是不是 revision 的造成者）。

## Followup

- 實作分兩個 release，第一個部署成功後才 merge 第二個；計畫在本機 phase plan（gitignored）步驟 9、10、12。
- 未部署的 R1 migration `7daffbb42a81` 不出貨；新 migration 接在 `8ac3b44460fc` 之後。
- trading 歷史列的配對鍵：請求的 `trading_state_id` 指到的列，且 `cause='operator'`、`actor` 等於 `requested_by`、`reason` 等於 `'kill: '`／`'resumed: '` 加請求的 reason；同一列有多筆符合時（同一操作者以相同 reason 重送 kill）取 `processed_at_ms` 最早者，其餘是 restate。後置斷言獨立檢查被選中的請求所連的列不早於它的 `processed_at_ms`（worker 先讀時鐘記處理時間、再讀時鐘寫列；restate 的請求在該列之後才處理）。
- 收尾時改寫 `backend/ARCHITECTURE.md` §7（分支上的 outcome 段落描述作廢）。
- 結案（2026-10-09）：R1'（PR #153，migration `5e820d6dc7da`，deploy ledger #271）與 R2'（PR #154，`41cec7caf291`，#273）皆已部署，部署前的 migration 演練在還原副本上通過；`backend/ARCHITECTURE.md` §7 與 `docs/runbooks/operations.md` 已改寫。

### Amendment Decision（2026-10-09）

- **新 revision 的 `source` 不再複製請求資訊**（Will 2026-10-09，選 A）：runtime 套用請求寫的 revision，`source` 只留 `amendment_digest`、`changes`；`request_id`、`requested_by`、`reason` 只經 typed `operator_request_id` 的 FK 取得。owner 腳本沒有請求列可 join，apply 必須帶 `--reason`，記在 `source`。歷史列不改寫：它們的 `source.request_id` 已由 `5e820d6dc7da` 後置斷言證明等於 typed 欄，之後以 typed 欄為權威。
- 不選「保留複本加約束」：`request_id` 可用本列 CHECK 綁，`requested_by`／`reason` 跨表只能靠 trigger，等於多一套機制守沒有讀者的資料。不選「維持現狀」：冗餘且無約束，D8 的「稽核文字」無從驗證。
- 依據：event sourcing 的 causation metadata 只存 id（[Marten](https://martendb.io/events/metadata.html)、[EventStoreDB](https://docs.kurrent.io/server/v24.10/features/projections/system.html)）；[Kubernetes ownerReferences](https://kubernetes.io/docs/concepts/overview/working-with-objects/owners-dependents/) 以 uid 為準。audit log 存快照的前提是來源會變或消失，請求列兩者皆否（欄位 grant、轉移 trigger、FK `RESTRICT`、不刪）。
- 延後：owner revision 的 typed `actor`／`reason` 與 CHECK（無請求的寫入必帶原因）。觸發：出現第二個能寫 policy 的人，或稽核需要對所有 revision 機械地回答「誰、為什麼」。

## Invariants

- 請求欄位寫入後 runtime role 不可改；`state` 只從 `requested` 轉一次終態。
- 每個對象同時至多一筆 `requested`（trading 的 kill 與非 kill 各一格），由 partial unique index 保證。
- 每筆帶 `operator_request_id` 的效果列與其請求同 scope、同對象；一個請求至多一個效果。

## Revocation Triggers

- 出現「決定紀錄不可改」的稽核或法規要求、決定的寫入者不只 bot、或決定要跨服務傳遞 → 重評 B。
- 出現第四種 operator 請求，或三種請求的授權規則收斂 → 重評併表。

## Review Notes

- 2026-10-08 實作前 design-review（獨立 agent）後修訂，ADR 尚未出貨：release 由三個併為兩個（Will 決定）；journal 改用兩條 MATCH SIMPLE FK 取代 generated 欄；`insert_request` 改用 `ON CONFLICT`；trading 配對的 tie-break 寫明。

## Related

- 來源：2026-10-08 Will 在 coding agent 對話中選定 E′，本 ADR 即原始紀錄。比較由一個未參與實作的 agent 從零起草（選項未預設），結論已寫進 Options 與 Rationale；outcome ADR 的兩份獨立草稿中，草稿 1 主張保留單表與 partial unique index，與本決定方向一致。
- 取代 [2026-10-08-operator-requests-insert-only-with-insert-once-outcome](2026-10-08-operator-requests-insert-only-with-insert-once-outcome.md)。
- 修訂 [2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md) D3' 與 [2026-09-26-runtime-role-writes-policy-toggle-under-trigger](2026-09-26-runtime-role-writes-policy-toggle-under-trigger.md) 的 G1，兩份的 Amendment 已改指本檔。
