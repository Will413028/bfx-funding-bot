---
title: 帳本狀態模型——按事實擁有者切分：bot 事實為 insert-once journal、venue 事實為已接受觀測，取代 event sourcing 與 projection replay
date: 2026-09-28
status: active
supersedes: "[2026-09-10-historical-replay-and-projection-audit-cutover](2026-09-10-historical-replay-and-projection-audit-cutover.md), [2026-09-20-capital-authority-bounded-read-by-prefix-hash](2026-09-20-capital-authority-bounded-read-by-prefix-hash.md)"
tags: [bfx-funding-bot, decision, event-sourcing, ledger, disaster-recovery, architecture]
---

# 帳本狀態模型：bot 事實 journal＋venue 觀測，取代 event sourcing

## Context

[2026-09-28-backend-capability-modules-enforced-boundaries](2026-09-28-backend-capability-modules-enforced-boundaries.md) D6 把帳本狀態模型留給本 ADR。現況：`event_log` 是唯一真相，`AccountEventWriter` 在 account advisory lock 內 append 並同步投影；另有 rolling prefix hash、snapshot＋tail 的資金讀取、DR 的 genesis replay parity 與 projection cutover 工具。`event_store/` 5,066 行＋`projection_cutover/` 2,434 行（`wc -l`）。

這套機制的實際成本：Halt 2 證明舊事件不足以重建現況（148,768 列只在 checkpoint，[2026-09-10-historical-replay-and-projection-audit-cutover](2026-09-10-historical-replay-and-projection-audit-cutover.md)）；09-20 資金讀取每次重推全部 intent 而擋掉每筆 offer，靠 prefix hash＋snapshot 補救（[2026-09-20-capital-authority-bounded-read-by-prefix-hash](2026-09-20-capital-authority-bounded-read-by-prefix-hash.md)）；歷史 CID 重用在 runtime projector 留下只為舊歷史存在的分支。而曝險本來就由每 ~90 秒的完整 venue snapshot 絕對覆寫，replay 算出的是「上一次觀測」，不是新知識。原始的 [2026-05-23-postgres-event-store-sot-migration](2026-05-23-postgres-event-store-sot-migration.md) D1 選的是 Hybrid、明確把完整 ES 列為過度設計，後來的 event-only projector 才把它推成完整 ES。

**約束**：

- `external` Bitfinex funding submit 不收 client id：送單前的 durable intent 與 UNKNOWN 是 venue 補不回的事實，必須保留且不可重送。
- `external` venue 是曝險權威，offers／credits／wallets 分開查詢、無原子快照（兩次觀測才接受）；WS 是可能漏訊息的 delta。
- `external` 真錢實盤中：每一步可部署；無外部使用者，允許計畫性 halt。
- `external` 既有 `event_log` 的 hash 已被 DR 驗證與 `capital_snapshots.covered_prefix_hash` 綁定，不可改寫。
- `external` 人力是 Will 加 coding agents：正確性要能由 CI／DR 機械判定，每行不變式程式碼都是長期維護成本。
- `inherited` write-ahead intent、UNKNOWN 不重送、per-account 串行寫入、REST reconcile 為正確性骨幹（[2026-05-27-reconcile-as-correctness-backbone](2026-05-27-reconcile-as-correctness-backbone.md)、[2026-08-31-production-integrity-staged-clean-cutovers](2026-08-31-production-integrity-staged-clean-cutovers.md) D4–D7）：仍成立，理由即上列 external 約束。
- `inherited` 資金授權讀取的成本不得隨歷史成長（09-20 R1）：仍成立，guard 2 秒 fail-closed。
- `inherited` 「money 要能由 log 重算」（05-23 D1）：**audit 半句仍成立，重算半句捨棄**——Halt 2 已證偽，且 live 狀態由觀測覆寫。

## Options Considered

- **基準 O：venue 鏡像＋write-ahead command journal（OMS 與交易 bot 常見做法）**：venue 擁有餘額／offer／credit／利息，本地鏡像；bot 只把自己才知道的事實寫成 append-only journal，定期對帳收斂（FIX ExecutionReport／drop-copy 對帳慣例；[NautilusTrader live reconciliation](https://nautilustrader.io/docs/latest/concepts/live/)）。
- **A. 維持完整 ES，把 projector 萃取成 Decider**：最保守；舊歷史分支永久留在 runtime，genesis parity 與 journal（~960 筆 snapshot／日）一起線性成長。
- **B. 以已驗證 epoch seal 為重建起點的 ES**（[Microsoft ES pattern snapshot 段](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing)）：拿掉 genesis replay，但 projector、事件 versioning、雙份 attempt 仍在，還新增 seal／verifier／receipt 機制。
- **C. 內部 double-entry ledger**（[Modern Treasury](https://www.moderntreasury.com/journal/accounting-for-developers-part-i)、[TigerBeetle](https://docs.tigerbeetle.com/)）：守恆可自我驗證；但本系統不託管資金，offered／lent／uncertain 是觀測不是轉帳，分錄只能由觀測差分反推，等於再造一份推導狀態。
- **D. 外部專用 ledger store（EventStoreDB／TigerBeetle 服務）**：一人維運多養有狀態服務，且無法與 intent 的 Postgres txn 原子提交。

## Decision

- **D1 真相按事實擁有者切分**：venue 事實（餘額、offer、credit、利息）的權威是**已接受的完整觀測**；bot 事實（decision、intent／attempt 與 outcome、provenance、uncertainty、operator 裁決、policy、trading state）的權威是 **insert-once journal 表**。不再有「由 log 投影而具授權力」的狀態。
- **D2 資金與曝險是純函式**：`f(最新已接受 capital snapshot, fence 之後且尚未反映的 attempts, 未解 uncertainty)`；venue offer／credit 鏡像在 snapshot 接受的同一 txn 直接 upsert，不經 projector。保留：兩次觀測、fence、金額指紋、account lock、UNKNOWN 不重送。
- **D3 append-only 由 DB 強制**：journal 與觀測表以 trigger 拒絕 UPDATE／DELETE（outcome 類欄位只能寫一次），並撤銷 runtime role 的 UPDATE／DELETE／TRUNCATE；CI 斷言權限。
- **D4 防竄改＝外部錨定**：每日把各 journal 有序內容的 digest 推到主機外（R2 evidence＋Telegram 通知 head），取代同 DB 的 per-row prefix chain。威脅模型寫明：偵測 bug、role 誤寫、restore 損毀與錨定窗口外的竄改；不防持有 owner 且在窗口內竄改者。
- **D5 DR 驗證**：restore 後逐張 journal 核對列數、max seq、digest 等於同一 PITR 點的外部錨定值；恢復交易前照舊做 fresh 完整對帳。不再做全量 replay parity。
- **D6 既有歷史**：切換前以現行程式對 `event_log` 做最後一次完整 replay、prefix 與 DR 驗證，final head 外部錨定後，`event_log`、`event_prefix_hashes`、`reconcile_observation`、projection archive 整批凍結到唯讀 `archive` schema，永久保留、不轉換。只把未終結的 attempt、uncertainty 與受管 offer 的 provenance 帶進新表。
- **D7 遷移（每段一個失效域）**：S0 shadow——新資金函式每 tick 與現行結果比對、只記差異，連續 14 天零個未分類差異；S1 expand——加 trigger、權限、provenance 欄位，同一 txn 內雙寫新表與現行 projector；S2 計畫性 halt 內切換授權來源，`event_log` 仍雙寫，回退＝部署上一個 digest；S3 穩定 4 週後停寫並凍結 `event_log`，刪除 projector、replay、cutover 與 prefix verifier，**S3 後不可回退、只能 forward-fix**。
- **D8 現在不做 double-entry**。

## Rationale

- **選基準 O 而非 A**：A 的成本是結構性的——每次改事件或 projector 都要證明「投影＝replay」，但保證正確的是 venue 對帳，replay 只證明 projector 自洽。代價：放棄已驗證的機制，承擔一次真錢遷移。
- **選基準 O 而非 B**：B 的論點是「可變 current-state 列成為真相會重開 dual-write 分歧，而送單前的承諾 venue 補不回」。D1／D3 回應它：bot 事實本身就是 DB 強制的 insert-once journal，不是可變狀態列；只有 venue 事實用鏡像，而鏡像本來就每 90 秒被觀測覆寫。B 保留的 projector 與新增的 seal 機制，正是本 ADR 要移除的維護面。
- **不選 C**：守恆由託管方 Bitfinex 承擔；在觀測之上再建分錄只多一份要對平的推導資料。代價：利息、費用的會計語意留到有需要時。
- **D4**：同 DB 的 hash chain 擋不住有權限的寫入者，外部錨定才構成證據；比透明日誌便宜，代價是竄改偵測只到錨定頻率的粒度。
- **放棄了什麼**：用重播精確重建任意時點內部投影的能力；時點查詢改由 journal＋觀測紀錄回答，仍能回答「某筆錢怎麼來的」。

## Expected Outcome

- 授權熱路徑的查詢數為常數（計數型測試釘住）；新增事件或欄位只需一般 migration，不再有 projector version、replay 相容或 cutover 工具。
- `event_store/` 與 `projection_cutover/` 的大部分程式在 S3 刪除（刪除後實測行數）；DR drill 時間與歷史長度無關。
- runtime role 對 journal 的 UPDATE／DELETE 在 DB 層被拒，由 CI 斷言。
- 對帳仍是唯一正確性骨幹；曝險不低估、UNKNOWN 不重送的不變式不變。

## Followup

- 遷移計畫（S0–S3）與 S0 比對器及差異分類表；journal insert-once trigger 的 RED 測試（UPDATE、DELETE、改 outcome 必須被拒）。
- 查 production `bfx_bot` 實際權限，D3 的 migration 以 integration test 驗證收斂。
- R2 錨定 digest 的格式、驗證工具與 object lock 可行性。
- 同步修訂 `backend/ARCHITECTURE.md` §5／§7／§9（I-ES、I-EW 退役）；確認 `PaperPositionLedger` 是否仍有授權 consumer。
- 14 天與 4 週兩個 gate 是判斷值，S0 後依差異資料再評。

## Amendment (2026-09-28)：S0 pre-flight 後的契約精化

S0 pre-flight 對照程式碼指出七處規格不精確；決策方向不變，以下取代對應敘述：

- **D2'**：資金函式的輸入是 `AcceptedCapitalBasis`（A/O/C、cell 歸屬、fence、已計入 attempts、接受時未解 uncertainty、eligibility evidence 的完整證據契約）＋fence 後 attempts＋未解 uncertainties＋applied policy＋scope＋單次注入的 clock/freshness；不是 raw REST payload。
- **D3'**：attempt、transport outcome、resolution 分三張 insert-only 表（outcome 與 resolution 以 UNIQUE 限首次寫入，衝突重複不當冪等），不在 immutable 規則內保留 UPDATE 例外；UNKNOWN→ACK 是一筆 resolution，不改 outcome。policy／trading state 的 head 指標與 operator outbox 維持現有「immutable request＋一次終態」機制。
- **D6'**：帶進新表的集合是「所有 live 資金歸屬的依賴閉包」（含 active credit 所需的已成交 offer→cell 關係、未完成 operator requests、未 reflected 的 acknowledged attempts），不只 active offer。
- **D7'**：S0 分兩臂回報——`fold_comparison`（共用既有 acceptance classification，只驗 fold）與 `acceptance_rederivation`（獨立重建歸屬，證據時點無法證明者標 `input_evidence_gap`、不計入 equal）；comparator 是獨立 process、只讀 `REPEATABLE READ` 視野、失敗只影響覆蓋率。S2 拆「準備」與「切換」兩個 release，切換不帶 migration，回退 digest 須事先證明能讀同一 schema head（`schema_head` 要求完全相等）。final head 在停止 legacy 寫入後才產生。
- **鏡像 terminal 規則**：區分「本次未觀測到」與「確證終結」，於 S1 契約明訂，不整段沿用舊 projector 的單調終結語意。
- **D4／D5 錨點**：定義一致的 capture point、每表 watermark 與跨表完整性；每日 digest 不等於任意 PITR 時點的 digest，DR 對照的是最近 capture point＋其後 tail。
- **Expected Outcome 修正**：「DR 時間與歷史長度無關」改為「不再支付 projector replay 成本」（全量 digest 仍讀歷史，有界需要分段封存，另案）；授權讀取除查詢數外，同時釘住載入列數與反序列化量。

## Amendment (2026-09-29)：S0 改離線歷史回放＋切換點同 snapshot 比對

- **Context**：`external` 無外部使用者、允許計畫性 halt（見 Context）——live 14 天 shadow 所服務的「不可停機、邊跑邊驗」前提不成立。prod 一個 scope 約 15.5k 事件，其中 `SUBMIT_OUTCOME_UNKNOWN` 僅 2 筆（2026-09-28 VM 唯讀查詢），live 窗口碰到的邊界案例少於既有完整歷史。S0-2 已交付 loader、baseline adapter、`compare_capital`（bfx-funding-bot PR #45、#47）。
- **Options**：A 常駐 live shadow（原 D7 S0；業界 parallel run 基準，適用不可停機的 live 系統，[GitHub Scientist](https://github.com/github/scientist)）——需新容器、專用 DB role、會停 bot 的 grants migration、部署工具的 soft-health 支援，S3 後全數刪除。B 離線歷史回放——在 isolated restore 副本上逐個歷史 acceptance 點重建 projection 並跑 `compare_capital`。C 只在切換點做同 snapshot 比對。
- **B 的實測前提（2026-09-29 VM 唯讀查詢）**：7,483 個 accepted snapshot（seq 7549 起）；3,712 筆 intent 中只有 9 筆帶新模型 attempt／`capital_authorization`（seq 9501 起）；UNKNOWN 僅 2 輪、皆結案 NOT_ACCEPTED；policy revision 無生效時間，歷史點的 policy 只有那 9 筆 intent 能證明。完整 B 需 as-of context、暫存 prefix＋增量 projector、隔離 restore 三個 PR，換來的真實情境只有 9 個 attempt，其餘點因兩臂共用 accepted basis 幾乎必然相等。
- **D7''（取代 D7 的「每 tick 比對、連續 14 天」與 D7' 的「comparator 是獨立常駐 process」）**：S0 = B-lite＋C。B-lite：在最新 isolated restore 上以一次性容器對 head 跑 `compare_capital`（C 的彩排，可重跑；不連 prod、不需新 role、無 as-of 機制）；歷史生命週期由合成 integration 測試覆蓋，並補一條重現那 2 輪 UNKNOWN 實際事件序列的測試。C 在 S2 halt 內、切換前對所有 live scope 執行，任一 different／error 即中止切換。gate＝未分類差異 0、error 0、每個 not_comparable 附證據。
- **Trade-off**：放棄以真實歷史逐點驗證（實際只有 9 個 attempt 可驗）與 S2 前 live 新資料的持續觀測，換得不建拋棄式的回放 harness 與 shadow 基礎設施；歷史覆蓋改由合成測試承擔。
- **Followup**：B-lite job 的 pre-flight（restore 生命週期、隔離 role 權限、防誤連 prod）；S1–S3 雙寫在無外部使用者下能否簡化為 halt 內一次切換，另行評估。

## Amendment (2026-09-29b)：S1–S3 改為單次 halt 切換，不雙寫

S1 唯讀 pre-flight（bfx-funding-bot `47cdc94d`）後，Will 2026-09-29 裁定。

- **約束**：`external` 無外部使用者、允許計畫性 halt（Will 2026-09-29 確認）；`inherited` `core/schema_head.py` 要求 DB head 與 image head 完全相等——只要帶 migration，任何選項都回不到切換前的 digest，D7 以為「回退＝部署上一個 digest」的前提只在雙寫期成立。
- **Options**：A 維持 D7 的 expand／dual-write／contract（業界零停機基準，[Fowler ParallelChange](https://martinfowler.com/bliki/ParallelChange.html)）——每個 legacy writer 同 txn 另寫新表，估約 700–1,300 行暫時程式，S3 刪除；切換後可回退，但前提是每個新事實（attempt、outcome、resolution、observation、provenance、operator 終態、policy、decision）都以舊語意寫齊。B 一次 halt：停所有寫入者 → `event_log` 最後一次 replay／prefix／DR 驗證 → migration 建 journal 與 ACL → D6' 閉包 seed → 現有 C＋新 reader 與閉包 digest 比對 → 切換授權 → 同 halt 凍結並封存 legacy。C 同 B 但 schema 與新程式先以非授權狀態上線，legacy 原地凍結，封存與刪碼另拆 release。
- **D7'''（取代 D7 的 S1–S3 與 D7' 的「S2 拆準備／切換兩個 release」）**：選 B。不寫雙寫程式。halt 以停止所有寫入者為準，不以 HALTED 狀態為準（reconcile、WS、operator outbox、cancel 在 HALTED 下仍會寫）。seed、比對、切換在同一次 halt 內完成：新授權寫入第一筆事實之前，任一 missing／different／error 都中止，還原 halt 前備份並部署原 digest；寫入之後只能 forward-fix。恢復交易前照舊做 fresh 完整對帳。
- **Trade-off**：相對 A，放棄切換後的回退能力、新舊模型並行處理 production 新資料的觀察期，以及雙寫期間暴露漏接寫入路徑的機會；換得不寫、不驗證、事後也不用刪約 1k 行的新舊語意對應程式。相對 C，放棄 schema 先在 production 驗證、封存分開失效；換得少一個 prepare release 與原地凍結的中間態。失效域集中在單次 halt，由 isolated restore 上的完整 halt 彩排承擔。
- **S1 契約必須修正的現況**（pre-flight 證據）：UNKNOWN→ACK 目前覆寫 attempt outcome，新模型只能寫一筆 resolution；完整 snapshot 未見目前被永久當成 terminal，新鏡像要分開「本次未見」與「確證終結」；D6' 閉包另含已 resolved 但尚未反映的 uncertainty、credit carry group（含 loan→split）、recent-fill attribution、trading state 的 auto-resume 時間窗；quarantine 以不可變的 opening／membership 事實保存，不可偽裝成 attempt。
- **Followup**：halt 彩排必須在 snapshot freshness（300 秒）內跑完 C，不得放寬；切換後部署工具要拒絕啟動 legacy digest；cutover scope 需反向核對未列出的 policy scope；cutover 唯讀 role 的驗證要涵蓋 column ACL、role membership、SECURITY DEFINER。
- **落地方式（Will 2026-09-29）**：journal／觀測／鏡像的空表、trigger、ACL 與 cutover 唯讀 role 在 halt 前先上線，不接任何 writer，新程式以休眠狀態進 main；halt 只做 seed、觀測、比對、切換與凍結。理由：main 自動部署並跑 migration、CI 跑 `alembic check`，而 C 需要的唯讀 role 本來就要 halt 前的 grants migration；這不是雙寫，也沒有 prepare release。
- **Revocation Triggers 修正**：「S2 後出現曝險低估：停在雙寫」改為「切換後出現曝險低估：kill switch halt，forward-fix，不回退 digest」。

## Amendment (2026-10-02)：S1 port 與 wire 契約另立子 ADR

S1-2～S1-3 的契約選擇（basis 只存事實、epoch 表與 DB 強制休眠、送單 CAS、不透明 token、讀模型取 basis）見 [2026-10-02-ledger-s1-port-and-wire-contracts](2026-10-02-ledger-s1-port-and-wire-contracts.md)；本檔決策不變。

## Revocation Triggers

- Bitfinex funding 開始接受 client idempotency key：重評金額指紋與 UNKNOWN journal。
- 出現外部使用者資金、績效費或分帳：另立 double-entry 會計 ledger 作下游 read model，遷移改零停機 expand/contract。
- S0 出現無法分類的差異，或 S2 後出現曝險低估：停在雙寫，回頭評估 B。
- venue history endpoint 的保留期或完整性不足以支撐 UNKNOWN 結案：本地觀測改存完整原文。

## Related

- 來源：2026-09-28 Will 與 coding agent 的討論，對照兩份獨立 subagent 草稿（一份主張 epoch seal ES，一份主張 journal＋venue 鏡像；兩者都否決 double-entry）；無外部來源文件。
- 取代 [2026-09-10-historical-replay-and-projection-audit-cutover](2026-09-10-historical-replay-and-projection-audit-cutover.md)、[2026-09-20-capital-authority-bounded-read-by-prefix-hash](2026-09-20-capital-authority-bounded-read-by-prefix-hash.md)（S3 生效，之前現行程式仍依它們運作）；修訂 [2026-08-31-production-integrity-staged-clean-cutovers](2026-08-31-production-integrity-staged-clean-cutovers.md) D4 的 event-only projector 部分與 [2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md) Expected Outcome 的「projection 可決定性重建」。
- [2026-05-23-postgres-event-store-sot-migration](2026-05-23-postgres-event-store-sot-migration.md) — 原 Hybrid 決定；[2026-05-27-reconcile-as-correctness-backbone](2026-05-27-reconcile-as-correctness-backbone.md)。
- [2026-09-28-backend-capability-modules-enforced-boundaries](2026-09-28-backend-capability-modules-enforced-boundaries.md) — D6 由本 ADR 決定；trading 模組依本模型切分。
- D7'''（切換程序）與 D6（既有歷史封存）由 [2026-10-06-switch-by-seed-and-retire-the-legacy-authority](2026-10-06-switch-by-seed-and-retire-the-legacy-authority.md) 取代：seed 後直接切換、豁免模擬 soak，legacy 表進 `legacy_archive`。

## Amendment (2026-10-03): 切換前的持續觀測由模擬 soak 補回

D7'' 放棄 S2 前 live 新資料持續觀測的理由之一是 shadow 基礎設施用完即丟；模擬器改為永久保留後，S1-7 前以獨立 simulation DB 上的限時 soak 作入場條件（門檻與範圍見 [2026-10-03-simulation-runs-the-ledger-on-a-simulated-venue](2026-10-03-simulation-runs-the-ledger-on-a-simulated-venue.md) D3）。D7'' 的 B-lite＋C 不變。

## Amendment (2026-10-04): 消失的 offer 依 id 查終態，查不到時以成交紀錄定量

- **事實（prod 唯讀 probe，2026-10-04）**：`/v2/auth/r/funding/offers/{Symbol}/hist` 的 start/end 篩選的是 MTS_UPDATE，不是 MTS_CREATE；同一 endpoint 接受 `{"id": [...]}`，會回傳指定的已結束 offer；歷史保留期至少涵蓋整個帳號期間（≥130 天）。Bitfinex 文件兩者都沒寫，屬於觀察到的行為。
- **問題**：舊做法在 client 端依 MTS_CREATE 過濾，查詢窗口只回推到上一輪 query 往前 60 秒。比窗口更早建立的 offer 結束時，ledger 看不到它的終態列，conservation 判 `unexplained_lending`。
- **Options**：
  - A 延伸共用窗口到消失 offer 的建立時間。
  - B 依 id 查詢消失的 offer。這是業界基準：FIX Order Status Request（35=H）、Binance `GET /api/v3/order`、Kraken `QueryOrders`。
  - 查不到時的處理另有四案：凍結整個帳戶直到查到；隔離該 offer 並把對帳放寬成區間；只把 trip 範圍縮到該 symbol；先寬限，再以成交紀錄定量。
- **決策（Will 2026-10-04）**：
  - **選 B**：窗口改依 MTS_UPDATE，消失的 mirror offer 依 id 查終態。
  - **查不到終態時**（venue 沒回、請求失敗、超過請求上限或無法解析），在本 process 內從第一次查不到起寬限 120 秒，期間該 symbol 維持 incomplete、每輪重試。
  - **寬限過後**，以本次 observation 的 funding trades 依 OFFER_ID 精確定出成交量：定得出來就照常對帳，單純撤單成交為 0；定不出或對不上，就走既有的 `unexplained_lending` → HALT → 條件解除後自動恢復（[2026-09-26-auto-halt-resumes-when-condition-clears](2026-09-26-auto-halt-resumes-when-condition-clears.md)）。
  - **mirror**：不寫假的終態，仍然「缺席不證明終態」。這類 offer 的狀態是「推定結束、未確認」，其 id 記在 accepted observation 的證據裡，basis 從那裡讀回。
- **Trade-off**：
  - 放棄的做法：凍結帳戶（違反放貸全自動，venue 一旦不回應就永久停擺）；區間對帳（放寬逐筆精確對帳，還要改兩條 quarantine 規則）；縮小 trip 範圍（等於拿掉第 3 級 HALT）。
  - 換到的：結果仍是逐筆精確對帳，而且只用既有的保護機制。
  - 代價：寬限期間整個帳戶的 observation 不被接受；trades 定不出量時會 HALT 一次。
  - 120 秒取自 UNKNOWN settle 窗口；venue 寫入歷史的延遲尚未量測。
- **重新評估條件**：
  - 寬限後的 fallback 每週超過 1 次：量測 history 延遲，調整寬限。
  - trades 定不出量反覆出現（`offer_end_judged` 告警帶 `undeterminable`；結果可由已存的 observation 與 basis 重算）：重新評估隔離方案。
  - Bitfinex 改變 by-id 行為或歷史保留期。
