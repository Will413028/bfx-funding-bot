---
title: Production integrity foundation — 分段 clean cutover（Release 0 → Halt 1 → Halt 2），UNKNOWN 為預設且不重送
date: 2026-08-31
status: active
tags: [bfx-funding-bot, decision, execution, event-sourcing, migration, safety]
related-commits:
  - "7678f93^..09761bc"
---

# Production integrity foundation — 分段 clean cutover，UNKNOWN 為預設且不重送

## Context

[2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md) 定了目標架構，但實盤 daemon 有六個跨邊界缺口：
公開 signup 且任何登入者都讀得到 process-global `BFX_ACCOUNT_ID` 選出的帳戶投影；daemon／webapi／vault／config
對「帳戶是什麼」各說各話；funding submit 遇 timeout／5xx 回 `failed`（venue 可能已接受）；`position_state`
並行 read-modify-write 無序列化；reconcile 遇未歸屬 offer 直接 raise 中止整個 snapshot；
`set_position_snapshot()` 在 event stream 外覆寫投影，live 狀態無法由 log 重現。本 ADR 決定怎麼補、按什麼順序切。

**約束**：

- `external` Bitfinex funding offer **不接受 client `cid`**：無法用冪等鍵安全重送（[Submit Funding Offer](https://docs.bitfinex.com/reference/rest-auth-submit-funding-offer)）。
- `external` 只有 operator 自有資金、無外部使用者：允許**計畫性停機**，不要求 zero-downtime。
- `inherited` write-ahead intent、single-writer advisory lock、append-only `event_log`、REST reconcile 骨幹（[2026-05-27-reconcile-as-correctness-backbone](2026-05-27-reconcile-as-correctness-backbone.md)）；仍成立：這些性質正確，缺口在它們之間的邊界。
- `inherited` 不留技術債（母 ADR 的 no-debt 目標）；仍成立：之後 control/execution seam、RLS、KMS 都疊在這層上。

## Options Considered

### 交付策略

- **基準. Expand/contract（parallel change）零停機遷移**：新舊 schema 並存、dual-write/dual-read，逐步切讀（[Fowler — ParallelChange](https://martinfowler.com/bliki/ParallelChange.html)）。
- **A. Big-bang**：authz、identity、event model、投影、UNKNOWN、quarantine 一次維護窗口全上。
- **B. 分段 clean cutover（選用）**：Release 0 containment（不停機）→ Halt 1 identity → Halt 2 execution-state；每段一個主失效域。
- **C. 先在舊字串 realm 上補 UNKNOWN／鎖，identity 之後再遷**：最快擋住 ambiguous submit。

### Ambiguous submit 處理

- **基準. 冪等鍵重送**：client 帶 idempotency key，失敗可安全重試（[Stripe idempotent requests](https://docs.stripe.com/api/idempotent_requests)）。
- **D. 例外即 failed、下一輪重送**（現況）。
- **E. 預設 UNKNOWN、永不重送、悲觀保留並只擋該 account/symbol（選用）**。

### 投影序列化

- **F. 對 `position_state` 加 `SELECT ... FOR UPDATE`**。
- **G. 每 account/environment 的 `AccountEventWriter`：xact advisory lock 內 append + 依 `event_seq` 投影 + 推進 cursor（選用）**。

## Decision

- **D1（spec §3／§10）**：採 B。順序 Release 0 → 驗證過的 offsite 備份 → Halt 1 → serialized projector → Halt 2（UNKNOWN＋quarantine）→ projection rebuild＋venue reconcile → bounded canary。**無 runtime dual-read、無 legacy realm fallback**。
- **D2（spec §4.1、§5）**：`ExchangeAccount`（immutable UUID）是 money aggregate root，不是登入 user；十張 money table 改 `exchange_account_id UUID NOT NULL` FK RESTRICT；`deployment_environment` 只是 stream 維度。
  歷史 realm 以簽核過的 mapping manifest 對應，舊 event payload 不改寫，decode 時 identity 取自 row 欄位。credential 以 account UUID 為 AEAD AAD，rotation 用新 row＋狀態轉移。
- **D3（spec §5.3–5.4，plan Release 0）**：authn 與 authz 分離：JWT 驗人，membership 授權帳戶；operator 只以 immutable user id＋`admin` role 認定（不用 email、不 fallback）。
  signup 在 Better Auth server hook 擋（藏 UI 不算）；他人帳戶一律回 non-enumerating 404。daemon 只吃 `BFX_EXCHANGE_ACCOUNT_ID`，無 `"default"`。
- **D4（spec §6）**：採 G。鎖整個 account/environment（不分 symbol）；transaction 內不打 venue；`set_position_snapshot()` 移除，reconcile 改 append full-account `VENUE_SNAPSHOT_OBSERVED`；
  offer／credit 以 venue object id 做 entity projection、terminal 單調；projector 純函式，rebuild 不讀時鐘、env、live API 或舊投影。歷史 v2 event 以 UUIDv5 推導 `event_id`，新 v3 帶 UUIDv4。
- **D5（spec §7）**：採 E。四值 typed outcome（Acknowledged／Rejected／OutcomeUnknown／NotSent），只有結構化拒絕證據才算 rejected；每個 decision 至多一個 attempt；`AccountCommandGate` 串起 guard→intent→transport→outcome；
  零候選或多候選都維持 UNKNOWN，absence 不是拒絕證明；`gross_exposure = offered + lent + uncertain`，可重複計算、不可低估。
- **D6（spec §8）**：未歸屬 active offer 持久化、計入曝險、只擋該 symbol，絕不合成 CID 或自動撤單；解除只能靠 domain event（operator 裁決需 fresh reconcile 與 operator id）。
- **D7（spec §10.4）**：venue 寫入恢復後禁止盲目 DB restore；流程是 halt → 保留 event log＋fresh snapshot → reconcile/adopt → forward-fix，只有證明其後無 venue mutation 才可 restore。

## Rationale

- **D1**：B 讓每段的 rollback 證據只對應一個失效域，相較 A 把 migration、auth、venue 狀態三個 blast radius 綁在一起、出事無法判讀；
  不選 C：臨時 identity 語意會讓 UNKNOWN／quarantine 新表在 Halt 1 再遷一次，違反 no-debt。不選基準：zero-downtime 的代價是整段 dual-read 相容分支，而外部約束已允許停機，買不到東西。代價：兩次維護窗口、兩次 canary 決策。
- **D2**：credential、nonce、曝險、reconcile 的自然邊界是交易所帳戶而非登入者；代價是十張表 backfill＋contract migration。
- **D4**：F 只防 lost update，不能在 submit ack、WS、REST snapshot、restart recovery 之間建立單一順序，且保留 delta 模型下 snapshot 與遲到訊息重複套用的問題；G 的代價是 per-account 寫入序列化，但不同帳戶仍可並行。
- **D5**：基準需要 venue 支援 client key，Bitfinex funding 沒有，重送就可能雙倍曝險；相較 D 的「少賺一輪」，E 的代價是人工／自動裁決負擔與暫時高估曝險，但低估曝險不可接受。
- **D6**：raise 中止整個 snapshot 會讓一個 orphan 蒙蔽所有其他幣別的曝險權威；自動撤單則可能撤掉 operator 手動單。

## Result

- `git log --oneline 7678f93^..09761bc`（Release 0、Halt 1 #7、v3 event identity #8、serialized projector #9、full-account observation、UNKNOWN #13、reconcile/quarantine #14）。
- Halt 2 evidence／canary gate 在後續 commits（`c97f27e`、`e0e20ce`）；Halt 2 實際 recovery 撞上歷史 CID 重用與舊 checkpoint，見 [2026-09-10-historical-replay-and-projection-audit-cutover](2026-09-10-historical-replay-and-projection-audit-cutover.md)。
- **O1**：D6 的「orphan 擋 symbol」已被 [2026-09-25-lending-envelope-replaces-probation-and-account-halt](2026-09-25-lending-envelope-replaces-probation-and-account-halt.md) D2 改為外來 offer 只記錄＋告警、不擋；D5 的結案方式由同 ADR D3a 金額指紋取代 fingerprint 比對。D5 的 UNKNOWN 預設與不重送不變。

## Invariants

- 沒有結構化拒絕證據的 venue 寫入結果一律 UNKNOWN；同一 attempt 永不重送。
- execution 投影只由 account event writer／projector 寫；live 與 clean rebuild 等價。
- 帳戶 scope 只來自 URL UUID＋membership，永不來自 process env。

## Revocation Triggers

- Bitfinex funding submit 支援 client idempotency key → 重評 D5，可改冪等重送。
- 出現外部使用者資金（無法計畫停機）→ D1 的 clean cutover 不再適用，後續 schema 變更需 expand/contract。

## Related

- 來源：`2026-08-31-production-integrity-foundation-design.md`（原文已不在 repo，本 ADR 即紀錄）
- 來源：`2026-08-31-operator-only-containment.md`（原文已不在 repo，本 ADR 即紀錄）
- 來源：`2026-08-31-exchange-account-identity-cutover.md`（原文已不在 repo，本 ADR 即紀錄）
- 來源：`2026-08-31-serialized-execution-projector.md`（原文已不在 repo，本 ADR 即紀錄）
- 來源：`2026-08-31-unknown-submit-orphan-quarantine.md`（原文已不在 repo，本 ADR 即紀錄）
- 母決策：[2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md)；執行完整性前置：[2026-08-23-funding-strategy-execution-integrity](2026-08-23-funding-strategy-execution-integrity.md)。
- 後續：[2026-09-23-venue-refusal-kept-before-classified](2026-09-23-venue-refusal-kept-before-classified.md)（5xx 仍 UNKNOWN）、[2026-09-04-offsite-dr-cloudflare-r2](2026-09-04-offsite-dr-cloudflare-r2.md)（Halt 2 DR gate）。
- Repo 現行規範：`backend/ARCHITECTURE.md` §1「Release 0 web API containment and Halt 1 identity」、§5、§9 I-EW／I-UA／Rollback after a venue write；runbooks `docs/runbooks/fresh-host-setup.md`、`docs/runbooks/rollback-after-venue-write.md`。已完成的 Halt 1 一次性操作文件於 2026-09-27 移除，歷史可用 `git show 83694e06:docs/runbooks/halt-1-exchange-account-cutover.md` 查閱。

## Updates (2026-09-28)

- [2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md) 以 bot 事實 insert-once journal＋venue 已接受觀測取代 event-only projector 與「projection 可決定性重建」；per-account lock、四值 outcome、UNKNOWN、orphan 與禁止盲目 restore 的決定不變。
