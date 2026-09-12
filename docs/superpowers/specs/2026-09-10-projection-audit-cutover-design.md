# 舊投影審計封存與 event-only cutover（方案 A）

## 決策與核准邊界

使用者已選擇 A：完整封存舊 checkpoints／投影，以新的完整 venue snapshot
建立切換基準，再使用 event-only projector。本文詳細設計已於 2026-09-10 獲使用者核准；
選擇方向不等於核准 production 套用、修改付費資源或恢復交易。

延續 `2026-09-10-historical-claim-replay-compatibility-design.md`。
實作必須包含已 review 的 `2595041`；目前它在獨立 branch，尚未整合 main。
本設計不重新放寬 historical claim policy、live identity uniqueness 或事件 hash。

## 已知證據與尚未證實的部分

7,545 筆歷史事件已在本機 PostgreSQL 18 與真正 isolated restore 中成功 replay，
canonical event hash 不變。DR runner 正確回報 `projection_replay_mismatch`：

| 投影 | 還原來源 | event-only replay |
| --- | --- | --- |
| offer_claims | 166 筆 | 166 筆，content hash 不同 |
| position_state | 2 筆 | 1 筆 |
| projection_heads | 1 筆 | 1 筆，content hash 不同 |
| reconcile_observation | 148,768 筆 | 0 筆 |

其餘固定驗證表一致。上述數量是既有凍結資料的證據，不可硬編碼為未來通過條件。
目前只確認差異表與 hash；claims／head 的逐欄差異尚未完成分類，不可直接視為
無害。未分類差異阻擋 apply。

舊 reconcile 刻意將完整對帳結果保存在 append-only checkpoint，CREDIT_CLOSED
則為 audit-only；因此舊 events 本來就不足以重建所有現況。新版完整 rebuild
會清空 scoped checkpoints 並從 VENUE_SNAPSHOT_OBSERVED 重建；舊串流尚無此事件。
不能把舊 checkpoint 當可丟棄 cache，也不能用今日快照假裝補出歷史明細。

實體還原 benchmark 約 88.7 秒，其中 pgBackRest restore 約 78.8 秒。
這不是包含完整 verifier 的成功 RTO，且位於已核准、與既有 operational target
對齊的 Halt 2 3600 秒門檻內。
process-max=4／同步 archive 在原先的 sub-minute benchmark 下，該既有比較未達標；不能斷言單一硬體瓶頸。

## 替代方案

- **A，採用：** 舊資料留在獨立 audit archive；active projections 只由事件重建。
  代價是新增封存完整性與 cutover 工具，但保留審計並維持新版 replay 邊界。
- **B，不採用：** 將 checkpoint 再納入 live replay 輸入。搬遷較少，但重新引入
  第二個權威來源，需另改已核准的 event-only 架構與驗收契約。

## 1. 封存契約

新增專用 audit schema，與 active projector 表分離，隨同一 PostgreSQL physical
backup 保存。它不是 runtime replay 輸入，不得因不參與 replay 而免驗證。
Alembic 只建立 schema／表／約束；資料封存與切換由獨立 operator command 執行，
禁止 migration 自動查詢 venue、載入 production secrets 或替換投影。

每個 cutover run 保存：

- UUID account、environment、run ID、release image、schema／projector version；
  原始 event count／head／canonical hash、封存格式版本與完成狀態。
- 原始 scoped offer_claims、position_state、projection_heads、reconcile_observation
  的完整欄位與主鍵；其餘 fixed projection tables 同樣捕捉，包括空表。
- 每表 schema 描述、筆數、排序規則、逐列與整表 digest。封存 hash 必須包含
  原始時間戳／surrogate ID；不得沿用會排除這些欄位的 projection hash。
- UUID、Decimal、timestamp、NULL 與 JSON 的版本化無損編碼；Decimal 不經 float，
  重複或無法解碼的 row key 拒絕。逐欄 round-trip 是驗收條件。

封存資料不含 API keys、KEK、database URL 或其他表的 credentials。
一般 bot／webapi role 無 archive 寫入與刪除權限；已完成 run 不允許覆寫。
資料庫管理員仍具高權限，不能宣稱 DB 權限本身提供絕對防竄改。
因此另將 manifest digest 保存在受保護的 operator evidence，獨立比對還原內容。
不新增自動 retention／清除政策；不刪除已封存歷史來縮短 restore。

## 2. 切換前診斷與 prepare

1. 確認正確 tailnet／VM／account／environment；維持 persistent halt 與所有
   trading／projection writers 停止。背景排程、API 與 autoheal 都納入檢查。
2. 對凍結資料做隔離 replay，產出逐欄差異分類：identity representation、
   歷史狀態、時間／sequence、缺少 symbol、checkpoint 來源。只可由證據分類；
   任一 unexplained difference 都停止，不加入 blanket allowlist。
3. 封存原始資料，驗證 round-trip、筆數、digest，取得包含封存資料的 R2 backup
   與 manifest。先在隔離 restore 證明 archive 完整，才允許替換 active rows。
4. 以最小 scope 的 operator 工具讀取完整 venue offers／credits／wallets，
   不啟動 strategy daemon，不提交／取消 offers。驗證 UUID credential、coverage、
   pagination、query 時間與既有 freshness／fence 契約；失敗立即停止。
5. 用實際新查詢建立新的 VENUE_SNAPSHOT_OBSERVED。明確包含受管理的 fUST／fUSD
   與查詢涉及的其他 funding symbols；不能因 API 省略零餘額就憑空認定完整。
   缺少幣別覆蓋證據時拒絕，不能只依舊 position_state 補出權威數值。

Operator evidence 只保存 normalized 非 secret 資料。新的 snapshot 是新的觀測，
不得回填舊 event timestamp／CID 或改寫原事件；也不得拿它證明過去 claim 已終結。

## 3. Transactional apply 與冪等性

Apply 顯式指定 prepare run、scope、expected original head/hash、manifest digest、
release／projector identity 及新 snapshot identity；沒有自動重試式交易啟動。

在同一 database transaction 與既有 account/projector lock 邊界內：

1. 重新驗證 halt、writers quiescent、原 head/hash、原投影封存 digest、snapshot
   freshness 與 archive 完整性。鎖取得後任一前置條件改變，整筆拒絕。
2. 沿既有 validated append 路徑寫入新 snapshot event；不得直接 SQL 插入跳過
   provenance、identity 或 snapshot validation。交易中暫時產生的投影不對外發布。
3. 呼叫核准版本的 full scoped rebuild，替換 active derived rows，不觸碰 archive
   或其他 account/environment。舊事件原始 prefix count/hash 必須保持一致。
4. 以獨立 temporary projection 比對全部 active counts/content hashes，再驗證
   venue exposure、head、snapshot coverage；全部通過才提交 cutover receipt。

重複同一已完成 run 僅驗證並回傳既有 receipt，不新增 snapshot 或覆寫 archive。
資料不同、run 部分完成或 snapshot 已 stale 時拒絕並要求重新 prepare。
不得把舊 baseline 改成新結果來冒充通過；切換後獨立捕捉新的 baseline，保留舊版。

## 4. 回滾與失敗處理

- **Transaction commit 前：** 任何失敗 rollback 新 snapshot 與全部 active 改動，
  原事件／投影仍在；已驗證的 prepare archive 保留，供稽核及再次診斷。
- **Commit 後、恢復交易前：** 保持 halt，不自動覆蓋 active rows 或刪除新事件。
  以保存的 cutover receipt／archive／backup 做隔離復原比較，再採 reviewed forward
  repair；不得讓舊投影搭配已前進的 event head 運作。
- **已有 venue 寫入後：** 本程序不提供自動回復舊 DB 或舊 image；立即 halt，
  重新對帳並進入獨立 incident recovery。舊 backup 不代表可以丟棄新增事實。

Pre-cutover physical backup 是最後安全復原證據，不是跳過對帳的 production rollback
指令。資料保存失敗、未知 exposure、archive mismatch 或 cleanup 失敗都不可放行。

## 5. DR 與 RTO 邊界

切換後的同目標 isolated restore 必須同時驗證：

- 原事件 prefix 完整、新 snapshot／切換後 event chain 完整；
- event-only replay 與所有 active projections 完全一致；
- archive 的 manifest、完整內容／筆數及 scope 與獨立 captured baseline 一致；
- image/schema/config identity、最小權限 verifier、斷開 R2 egress、清理證據。

不能只驗證 archive 表存在，也不能把舊資料移到驗收看不到的地方。
DR verifier 只新增必要 archive SELECT 權限，不可取得 production secrets／寫入權限。

RTO 優化是獨立量測工作，不是本封存方案的預期收益。計時包含既有 runner 定義的
完整 restore／recovery／bootstrap／replay 與新增必要 archive 驗證；cleanup 仍須
通過獨立 deadline。不得把成本移到計時前或使用預先還原的 volume 冒充冷恢復。
先量測每階段與 CPU／I/O／傳輸，再按單一變數提出優化及 pytest regression。
變更 OCI 容量／性能計費、備份範圍、retention 或 RTO 定義需另行核准。

維持 Halt 2 **RPO <=300 秒、RTO <=3600 秒**；此為與既有 operational target
對齊的已核准門檻。
若實測仍無法達成，明確回報阻擋，不承諾恢復日期、不放寬門檻。

## 6. 測試與交付邊界

使用 pytest：封存無損編碼／digest／跨 scope、immutable completed run、重複 apply、
並行 writer／head 漂移、stale／partial snapshot、缺幣別覆蓋、逐步故障注入與原子
rollback、prefix 不變、其他帳戶不變、archive tamper／缺列、最小權限與 cleanup。
真 PostgreSQL integration 必須包含 prepare→apply→重複呼叫→新 baseline→還原驗證。
私有真實資料僅在 operator 隔離環境演練，不寫入 repository fixtures。

依序交付：逐欄診斷、archive/Alembic、operator dry-run/apply、archive-aware DR 驗證。
獨立 review 與隔離演練通過後，提交明確 scope／backup／receipt 給 operator 核准
production apply。不得把實作完成當作套用授權。

最後仍須完成 runtime UUID／KEK／operator identity 遷移、移除 legacy secret fallback、
auth／uncertainty／完整 exposure 驗證、fresh DR 及既有 bounded canary／兩輪 fresh
reconcile。任何一項未過，都維持 halt；本設計不核准恢復交易。
