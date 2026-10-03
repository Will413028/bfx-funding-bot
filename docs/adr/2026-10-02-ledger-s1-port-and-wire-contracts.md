---
title: 帳本 S1 的 port 與 wire 契約——basis 只存事實、DB 強制休眠、送單 CAS、不透明 token、取 basis 為讀模型
date: 2026-10-02
status: active
tags: [bfx-funding-bot, decision, ledger, architecture, api-contract, concurrency]
---

# 帳本 S1 的 port 與 wire 契約

## Context

[母 ADR](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md) 定了「bot 事實寫 journal、venue 事實取已接受觀測」，09-29b Amendment 定了單次 halt 切換、空表與休眠程式先上線。S1-2～S1-3（PR #61–#85，2026-09-30～10-02）把 21 組 consumer 改成依賴 port、由開機時選 legacy 或 ledger 實作；過程中出現幾個母 ADR 沒回答、且之後切換時會被問「為什麼」的選擇。本檔只記這些契約。

**約束**：

- `external` 系統未上線、只有一位 operator、允許計畫性 halt（Will 2026-09-29，10-02 仍成立）。
- `external` frontend／webapi／bot 同一個 revision、同一次 compose up 部署（`docs/runbooks/deploy.md`），版本 skew 只有秒級。
- `inherited` 不造合成序號（S1-2c ruling）：ledger 沒有 event_seq，任何「序號」欄位在切換後都沒有真值；仍成立，因為 ledger 的身分是 observation／attempt UUID 與 clock revision。
- `inherited` 放貸全自動（AGENTS.md，#59）：正常流程不得有人工步驟；仍成立。
- `inherited` 一個 scope 一把 advisory lock（`core/writer_lock`）：ledger 與 legacy 共用同一把；仍成立。

## Options Considered

**(a) basis 與 policy 的關係**
- **基準. 下單當下套用限額、曝險取自成交與掛單**：pre-trade risk control 的標準形狀（SEC Rule 15c3-5 market access rule：限額在送單時檢查，版本記在單上）。
- **A. basis 綁一個 policy revision**（S1 Q2 原 ruling）：一個 scope 可有多幣別 policy head，單欄位裝不下，且沒有 head 的 scope 記不了 blocked basis。
- **B. basis 只存 venue 事實與歸屬，policy 每個 symbol 在讀取時套用**（選用，即基準）。

**(b) 開機選 authority 的依據**
- **基準. 環境變數／feature flag**：常見做法，切換只要改設定。
- **A. 環境變數**：DB 不知道目前是誰當家，legacy 映像在切換後仍可能被部署工具啟動並寫入。
- **B. insert-only `capital_authority_epoch` 表＋ledger 表的 INSERT trigger 在 epoch≠ledger 時拒絕 runtime 寫入**（選用）：休眠由 DB 證明，部署工具也讀同一張表。

**(c) 送單授權的並發控制**
- **基準. optimistic concurrency（版本 CAS）**：[Fowler, Optimistic Offline Lock](https://martinfowler.com/eaaCatalog/optimisticOfflineLock.html)。
- **A. 照 legacy 在 lock 內重算資金**：每次送單都重讀全部，ledger 端沒有便宜的增量讀取。
- **B. scope 鎖內 CAS：最新 query＝token 的 query、clock＝token 的 revision、basis 相符、該 symbol 的 policy head 相符**（選用，即基準）。

**(d) 對前端的 token 形狀**（Will 2026-10-01）
- **基準. 不透明字串**：HTTP ETag 的作法（[RFC 9110 §8.8.3](https://www.rfc-editor.org/rfc/rfc9110#section-8.8.3)），client 只回送、不解析。
- **A. 保留數字欄位、另加新欄位**：ledger 下數字欄位沒有真值，只能填合成序號。
- **B. 版本化 union**（legacy／ledger 兩種形狀）：適合 dual-write 或長時間 skew，這裡兩者都沒有。
- **C. 不透明字串＋另加觀測時間顯示欄位**：要擴充兩個 adapter 的 port。
- **D. 不透明字串**（選用）：status `basis_token: string`、uncertainty `evidenceRef: string`、offers `offerKey: string`。

**(e) ledger 下的讀模型**（Will 2026-10-01）
- **A. 即時 mirror**：最新，但不是 authority，和送單用的資金數字可能對不上。
- **B. 最新 accepted basis**（選用）：positions 的 reserved／realized 取 basis 的 symbol 列。
- 前端沒用到的 `openedEventSeq`／`resolvedEventSeq`／`reconcileEventSeq`／`lastEventSeq`：**刪除**（選用）而非保留填 null。

## Decision

- **D1** basis 只存事實（venue 觀測＋歸屬＋事實層 block），policy 依 symbol 在讀取時套用；送單的 attempt 記下授權當下的 policy revision（#64、#66）。
- **D2** authority 由 insert-only epoch 表決定，開機讀一次、對不上就拒絕開機；ledger 表的 runtime 寫入在 epoch≠ledger 時由 trigger 拒絕（#69）。
- **D3** 送單以 scope 鎖內 CAS 授權（含 policy head，因為 policy 變更不推進 clock），失敗回 refused、不寫入（#72）；port 位於 ledger facade，bus 通知不帶 CID（#73、#80）。
- **D4** 所有對外 token 一律不透明字串（#83；offers 的 `offerKey` 在 3f2）。
- **D5** ledger 讀模型取最新 accepted basis；未使用的 EventSeq 欄位刪除（#85）。

## Rationale

- **D1**：選基準而非 A——A 的單欄位裝不下多幣別 policy，且會讓「沒有 policy head」變成無法記錄的 basis；代價是讀取時多一次 policy 查詢，且 basis 不能單獨說明「當時准不准」，這由 attempt 上的 policy revision 補足。
- **D2**：選 DB 表而非環境變數——切換後最危險的是舊映像被拉起來繼續寫；表加 trigger 讓這件事在 DB 層失敗，不靠部署流程記得。代價是多一張表和一組 trigger，且 owner 必須保有豁免（seed 與測試）。
- **D3**：選 CAS 而非 legacy 的 lock 內重算——ledger 的資金讀取已是有界讀取，CAS 只比較識別值，鎖持有時間短；代價是同一輪連續送單時，第一筆推進 clock 後第二筆必須重讀（只讀 DB），失敗時 refused 而非排隊。
- **D4**：選不透明字串而非數字或 union——skew 秒級、只有一位 operator，union 的過渡成本沒有對應收益；代價是 status 頁少了一個可讀數字（實際沒有元件顯示它）。前端改成「非空就回送」，避免數字型別判斷在 ledger token 下把所有裁決按鈕靜默停用。
- **D5**：選 basis 而非 mirror——頁面數字和送單授權同源，代價是兩次 accept 之間最多落後一個 reconcile 週期（約 90 秒）；刪欄位而非填 null，因為填 null 的欄位只會讓下一個讀者以為它有意義。

## Expected Outcome

- 切換時前端不需要再改契約；legacy 期間 token 內容是十進位字串，行為與改動前相同（#83 golden 測試）。
- 切換後舊映像若被啟動，ledger 表的寫入在 DB 層失敗，而不是靜默寫進休眠的表。
- 同一 scope 不會有兩筆送單基於同一份資金讀取同時通過授權（#72 競爭測試）。

## Followup

- ~~3f2：offers 改 `offerKey`、positions 讀 basis（webapi 依 epoch 選讀模型）~~ 完成（#87、#88，2026-10-03）。
- 3e：POST 接受 `ledger:v1:obs:` evidence ref、worker 寫 journal `operator_request_id`（S1-7 前必須完成）。
- 3e：ledger adapter 的送單重試（CAS 失敗重讀一次、只重試一次）與 boot 遇到非 accepted decision 的策略。

## Amendment (2026-10-03)：positions 改互斥分量；webapi 權限由 migration 擁有 exact allowlist

**起因**：3f2 pre-flight 發現 D5 的映射 `realized ← credits + unattributed_credits` 重複計算——`unattributed_credits` 本就含在 `credits`（`trading/capital.py:91` 拒絕 unattributed > credits）；legacy wire 的 `reserved`／`realized` 實際裝的是 `offered_amount`／`lent_amount`，卡片把借出本金顯示成「已實現」。3f2b 的 mutation「offers 讀 `execution_decisions`」存活：測試 template 給 `bfx_webapi` 全表 default privileges，prod 沒有（`docs/runbooks/fresh-host-setup.md` §1a、prod `pg_default_acl`）。約束不變，另加 `external` 系統未上線、wire 可一次改前後端（Will 2026-10-02：「不要留技術債」）。

- **positions wire**——基準：互斥分量＋恆等式（[Stripe Balance object](https://docs.stripe.com/api/balance/balance_object) 的 available／pending 分桶）。A 只修映射 `realized ← credits`：改動最小，誤導名稱留著。B 讀取層組合：每個 consumer 自己加總，這次就是這樣錯的。**選基準**（Will 2026-10-02）：`available`、`offered`、`lent`（= credits），`unattributedLent` 標明是 `lent` 的子集合、不參與加總；ledger 取最新 accepted basis，legacy 取 canonical `*_amount`，`unattributedLent` 在 legacy 為 null。代價：前端、i18n、fixture 同 PR 改（#88）。
- **webapi 的 intended amount**——基準：寫入時就有型別的欄位（[PostgreSQL generated columns](https://www.postgresql.org/docs/current/ddl-generated-columns.html)）。A 授權 `normalized_payload`：API 綁內部下單格式。B owner 擁有的 view：多一個 DB 物件、解析放 SQL。**選基準**（Will 2026-10-02）：`intended_amount GENERATED ALWAYS AS ((normalized_payload->>'amount')::numeric) STORED NOT NULL`＋finite 且 ≥ 0 的 CHECK，webapi 只授權這欄（#87；NOT NULL 是因 CHECK 對 NULL 放行）。
- **D6 webapi 權限**——基準：權限寫成程式碼、最小權限（[PostgreSQL GRANT](https://www.postgresql.org/docs/current/sql-grant.html)）。A 靠主機 default privileges：測試與 prod 不一致，正是存活 mutation 的成因。C 獨立 API schema 只露 view（[PostgREST schema isolation](https://docs.postgrest.org/en/stable/explanations/schema_isolation.html)）：隔離最徹底，但要重寫所有讀取路徑。**選基準**（Will 2026-10-03，即選項 B）：migration `d0e1f2a3b4c6` 先 REVOKE ALL（table／sequence／function）再授回明確清單；原本由 runbook 手動 grant 的 `api_keys`、`user_configs` CRUD 與 `user_profiles` UPDATE 一併收進清單。代價：之後每個授權 webapi 的 migration 都要同步清單常數，由 `test_webapi_privilege_allowlist.py` 擋漏（#89；部署前後 prod 權限 diff 為空）。

D5 其餘部分（讀模型取 basis、刪 EventSeq 欄位）不變。Revocation：若出現第二個對外 API 角色或外部使用者，重評 C。

## Revocation Triggers

- 出現第二個 operator 或外部使用者、或前後端不再同 revision 部署 → 重評 D4（可能需要版本化 token）。
- basis 落後造成 operator 誤判（頁面數字與 venue 明顯不符且影響操作）→ 重評 D5，考慮 mirror 作為輔助顯示。

## Related

- 母 ADR：[2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md)（09-29b Amendment：單次 halt、空表先上線）
- PR：Will413028/bfx-funding-bot #64、#66、#69、#72、#73、#80、#83、#85、#86–#89（`gh pr view <n> -R Will413028/bfx-funding-bot`）
- 測試權限保真度：對應通用教訓「test fixture 的鑑別力」（本案為其中一例）
