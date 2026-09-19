# Dynamic capital policy and release-scoped canary

日期：2026-09-19。狀態：設計已核准，使用者已授權自主實作與技術部署；production acceptance 尚未完成。

## 1. 目標與範圍

正常放貸使用既有帳戶、已啟用幣別的實際可用資金；使用者未設定保留時 reserve 為 0。取消需要人工同步的固定總額 cap，保留執行前資金檢查、單筆／cell 分配限制、授權、持久化 halt、資料 freshness、UNKNOWN 阻擋及 audit。

將正常放貸資金政策與 release 的一次性真錢驗證分離。這次只改相關配置、資金計算、命令邊界、release 證據與部署流程；延用 PostgreSQL、現有 account command gate、event store、permit 與 account configuration。不上新服務、不增加硬體、不建立通用規則引擎。

「無付費使用者」不等於空白環境：已有真錢 event/attempt/offer 歷史、持久化 halt、vault、DB 與備份。重構不得清空帳務或把 SQL/DB rollback 當成交易所操作的撤銷。

正常模式允許全部合格資金參與放貸，因此帳戶內全部該幣別資金仍可能承擔交易所、借貸與軟體風險。動態資金檢查不是最大損失保證，canary 成功也不是策略獲利證明。

## 2. 實際問題與選擇

本機基線為 `c7ee400`，以下是 repo 檢查結果，並非本輪 VM 現況驗證：

- `deploy/vm/canary.env` 同時承載長期 live profile 和 Halt 2 one-shot：amount/cap=150、allocation fallback=0。
- `backend_py/configs/safety.canary.yaml` 為 fUST=10000、fUSD=0；`cells.canary.yaml` 有 a30/p2 兩個 cells。
- `daemon.py` 的 one-shot 檢查要求恰好一個 cell，caps map 恰好等於一個 symbol 的 canary cap，allocation scalar 也必須相等。上述 normal profile 與 one-shot contract 不相容。
- 正常 allocation 中的 scalar=0 本來是 fallback，不等於全域停機。因此只把它改成 150 無法修復責任混用。
- `CanaryPermitRepository` 已有 durable consumption 與 halt binding；`AccountCommandGate` 已有 serial execution 與 write-ahead intent。應延伸既有保證，不建立平行執行通道。
- deployment 現在在 VM pull/build，卻要求預先提供 image digest；重建結果與預核准 artifact 容易脫節。

| 方案 | 結果 | 決定 |
| --- | --- | --- |
| 同步所有固定 cap | 可局部解開啟動，但入金、正常策略與 release 還是互相耦合 | 不採用 |
| 只信任交易所 available，移除其餘資金控制 | 無法阻止本機並行重用餘額、重送與異常策略分配 | 不採用 |
| 單一動態 CapitalPolicy + 獨立 ReleaseCanary | 集中計算，release 限制只約束自己的命令；需明確 migration | 採用 |

## 3. 政策所有權與型別

`CapitalPolicy` 是 application code 中的純計算邏輯，不接受 DB 中的公式或任意 actions。輸入是 validated、immutable 的 effective account policy revision 與 account/symbol capital snapshot。

每個 `(exchange_account_id, deployment_environment, symbol)` 的有效政策包含：

- `enabled: bool`：明確是否允許新放貸。disabled 與 reserve/cap=0 不再混用。
- `reserve_amount: Decimal >= 0`：以幣別原生單位表示；新政策預設 0。
- `allocation_mode: all_available`：本版只有此模式，不預建百分比／固定總額選單。
- `max_cell_fraction: Decimal`，`0 < value <= 1`：延用既有 70% cell concentration 語意；不因本次重構提高。normal cells 與 strategy params 維持既有行為。

沿用既有 account draft → validated applied revision 的邊界。draft 不直接影響 daemon；每個 ready decision 綁定 applied revision/digest，submit 前重查 revision，過期就重新評估。reserve 只有一個生效来源，不從 user_configs、env 與 YAML 同時讀取。

不存在 policy、無法讀取 applied revision、非法數字（NaN/Infinity/負值）、未知 schema version 均 block。預設 reserve=0 是建立有效 policy 時的明確值，不是啟動時資料遺失的 fallback。

首版只啟用 fUST；fUSD 明確 disabled，但 full-account reconcile 仍需觀察所有持倉。正常模式可使用 a30/p2；canary 從其合法集合選一個 cell。

## 4. 資金模型與命令邊界

按幣別計算；禁止直接把 USD、USDT 或其他資產的數字相加。只使用 funding wallet，沒有自動轉帳或借入資金。

給定同一份已完成且新鮮的 full-account snapshot：

- `A`：交易所可用餘額，已排除交易所凍結的 offer／loan。
- `L`：本機 durable commitments 中，尚未被該 snapshot 證明已反映在 A 的金額。
- `R`：有效政策的 reserve。
- `T`：同 scope funding capital，包括 available、offers 與 lent，依 canonical projector 的去重分類取得，不能再加一次本機對應 reservation。
- `E_cell`：cell 已借出、掛單與尚未反映於 snapshot 的 commitments，按 attempt/offer identity 去重。完整 snapshot 中已確認、但缺少真實 cell provenance 的 credits 作為 shared unattributed exposure，保守加入每一個 cell 的 concentration 評估；在 A/L/T 的資金計算仍只算一次，不能虛構 ownership。

```text
spendable = max(0, A - L - R)
cell_limit = max(0, T - R) * max_cell_fraction
cell_headroom = max(0, cell_limit - E_cell)
new_offer_amount <= min(spendable, cell_headroom)
```

策略建議還要通過利率、期間、book freshness、venue minimum/precision 與既有 eligibility guards。只向下量化金額；不足 minimum 回 `insufficient_deployable_funds`，不得為達最小額而突破 headroom。venue minimum 由 adapter 的已驗證規則提供，不能把目前程式中的 150 認定為永遠有效的交易所規則。

這是「新增可放貸金額」，不是要求每輪重新送出全部帳戶總額。分配器與 pre-submit guard 共用同一 `CapitalPolicy` evaluator；不能由兩套公式分別決定 target 和安全上限。

L 的扣抵必須有 snapshot fence、attempt identity 與 event sequence 的對應證據。不能用「時間較新」猜測某筆已被反映；無法分類時 block，不能清零，也不能同時從 A 與 E 重複扣抵。snapshot ingestion 需保存查詢前 command fence，接受時在相同 account lock 下確認查詢期間沒有衝突命令；snapshot append 序號不能充當查詢前 fence。人工掛單／外部 auto-renew 產生的未知活動沿用 quarantine/uncertainty 流程，不能默認可管理。已確認 credit 僅缺 cell provenance 與 execution UNKNOWN 必須區分：前者採上述保守 concentration，後者阻擋送單。

命令執行延用 account-scoped single writer 與 AccountCommandGate：

1. 驗證 writer ownership、halt、scope、policy revision 與 snapshot fence。
2. 在既有序列化邊界重新評估所有 guards 和 capital headroom。
3. 同一 DB transaction 內建立 durable intent/reservation 及相關決策記錄；以 DB 鎖／constraint 封住跨程序競爭。
4. commit 成功才呼叫交易所。不可在 network call 期間持有 DB transaction；single-writer fencing 與 durable commitment 必須覆蓋該段時間。
5. `acknowledged` 轉成已知 offer；`rejected/not_sent` 按證據釋放；`unknown` 保留 pessimistic commitment 並阻擋相關 scope，不自動重送。

程序重啟由 durable events/attempts 恢復 L，不能僅依 in-memory lock/counter。cancel acknowledgement 不立即視為現金回流；以完整 reconcile 證明可用金額後才重用。

範例（忽略 cell headroom 以單獨檢驗餘額）：A=1000、R=100、L=200 時 spendable=700；若 snapshot 已包含這筆 200 的凍結，A=800、L=0，結果仍為700。兩筆並行命令不能各花一次700。

reserve 增加或帳戶資金下降到低於已借出資金時，只停止超額的新單；不召回貸款、不自行轉帳或取消既有掛單。入金在 fresh reconcile 後自動增加可用額，不需人工提高固定 cap。

## 5. 執行安全與績效

保留持久化 halt、唯一 writer、account ownership、授權健康、資料新鮮度、duplicate prevention、完整 coverage、UNKNOWN、audit-before-submit、rate/period validity。normal live 與 canary 共用相同 money boundary。

既有 loss/drawdown thresholds 在本輪不放寬，也不宣稱能保證最大虧損。存提款與內部轉帳不等於策略損益；若來源無法識別，報告應標示原因，不能直接清除 halt 或重設高水位。績效／現金流量測的全面重設另列工作，不夾帶到資金政策重構。

舊「提高固定 cap 需 G3」流程在新 all_available 政策啟用後沒有對應操作；其策略績效 evidence 與未通過的結果仍保留。transition report 必須揭露有效 exposure 可能增加，不能把 mode conversion 偽裝成等價 migration。策略變更、啟用新幣別或提高 concentration 不屬於本次核准。

## 6. ReleaseCanary：一次性驗證

`paper/shadow/live` 表示執行模式。`canary` 從長期 mode 移出，成為特定 release 的 validation session。normal live 仍有完整 guards；不存在因移除 canary phase 而跳過 guards 的分支。

擴充既有 durable permit，綁定：release id、artifact identity、config/policy digest、projector/schema version、canonical account/environment、halt epoch、symbol、cell、strategy、單一 `max_amount`、expiry、operator 及 durable attempt。account 來自核准 session，同 canonical account 逐項驗證；不再請人複製第二個 account env。

`max_amount` 是此 release 的單次風險額度，與 normal account capital 無 equality 約束。送出 amount 由正常 policy/venue validity 算得，且不得超過 max_amount；實際 exact amount 在 consumption 前寫入 permit/attempt，後續不能改。preview 顯示最小合法驗證單與可用資金；不足則 block，不默默放大。金額與 expiry 必須在核准 session 中明確存在，不用無來源的 magic default。

首版維持最多一次 venue submit；相同 halt epoch 不得透過重啟、改 release id 或新 permit 取得第二次送單。permit 在 venue boundary 前 durable consume；consume 後即使 crash，也不得 automatic retry。freshness、expiry 與 hash 在 consume 前檢查，不只在建立時檢查。

```text
prepared -> authorized -> consumed -> observed -> validated -> promoted
                  expiry/failure/UNKNOWN -> blocked (halt retained)
```

狀態轉移由 code 驗證與 DB 保護；timeout 不等於成功。`validated` 要求 durable ACK、venue offer identity、完整 outcome event、outcome 後兩個不同 fence 的完整 reconcile、projection parity、零未解 UNKNOWN 與相符 artifact/policy。單次測試只證明此次 execution path，不代表全部故障或策略收益已被驗證。

觀測不足或 expiry 過期保持 halted；允許在無新 venue write 下繼續讀取證據，但不得自行延長送單授權。expiry 限制送單授權，不讓已送出的 attempt 失去追蹤；promotion 仍需當下 fresh readiness。所有 terminal path 重新確認持久化 halt；halt 寫入失敗需停止 writer 並明確報錯。

目前 Halt 2 恢復仍須保留實測 backup RPO ≤300s、isolated restore RTO ≤3600s、event continuity、schema/projector parity 與 source identity 等既有 gate。它們屬於本次資料恢復 acceptance，不因改名成 release session 而移除，也不要求每輪正常放貸重新跑 restore。

`promote` 使用既有 authenticated operator control，驗證 release 與目前 runtime/policy/epoch 一致及正常 readiness；以 audited transaction 記錄 promotion 並解除對應 halt。validated 不自動等於 resume。promotion 後 normal live 不需要重複核准同一 release 的 canary；重啟保留 promotion。新的 executable/config/policy digest 必須重新判定授權，不能沿用不相符證據。

rollback 停止新增命令、保留 audit、fresh reconcile，優先 forward-fix。不能用 image rollback 或 DB restore 假裝已撤回貸款；舊 binary 若不支援新 schema/policy 必須拒絕啟動。

## 7. Artifact 與設定來源

release tool 建立一次候選 image，記錄 source revision、platform、image ID／OCI manifest digest（型別分開，禁止互相比對）與 config digest，驗證後部署同一 artifact。依現有單 VM 可保存本機 immutable image；不要求新增付費 registry。

部署不再 pull moving main 或重新 build 核准候選。`BFX_EXPECTED_IMAGE_DIGEST` 由 release manifest 提供，不是人類手填欄位。image label 本身不證明執行檔版本；需一起驗證 build provenance 與被啟動的 image identity。

敏感值仍在 protected files；非敏感 policy 及 release metadata 具 schema/version。archive credentials、operator credentials 與 auth/TOTP provisioning 不在這份重構範圍。

## 8. Migration 與刪除清單

在停止新增送單、完成備份及 fresh reconcile 後進行一次明確 cutover。支援 dry-run，輸出舊來源、有效值、新 policy、disabled symbols、exposure/headroom 差異和所有無法轉換項。

1. 建立 typed policy 與必要的 release/permit schema migration，使用 Alembic；保留歷史 event 和 permit audit。
2. 為既有 account 建立 applied revision：fUST all_available、reserve=0、既有 concentration；fUSD disabled。先呈現轉換差異，再在既有 operator apply 路徑生效。
3. planner、command gate、status/dry-evaluate 接入同一 evaluator；政策修改使用 revision check，不自動啟用 draft。
4. 將 one-shot runner 與 normal daemon startup 分離；canary scope 限制作用於該命令，不要求 normal cells/caps map 改成 canary profile。
5. 切換 build-once manifest 與 live mode、runbook、env examples、frontend 顯示及 ops scripts。
6. 移除 money path 的 `BFX_ALLOCATION_CAP_USDT`、`BFX_BALANCE_BUFFER_USDT`、per-symbol cap/buffer fallback；reserve 與 concentration 改由單一 applied policy 供應。
7. 移除人工設定的 `BFX_CANARY_ACCOUNT_ID`、duplicated amount/cap/scope env 及手填 expected digest。permit/session 成為其唯一來源。
8. 移除舊 canary phase/profile 的 runtime 分支；歷史 backtest/research 資料可保留舊名稱，但不能作 live fallback。遇到舊 runtime 變數直接報 migration error，不能雙讀／猜優先順序。
9. 更新 `backend_py/ARCHITECTURE.md`，清理舊 runbook 與 wizard 的失效命令；保留歷史證據的 schema reader，但不允許舊證據授權新版本送單。

本輪 spec 不改 production；實作驗證後的 cutover report 需明確區分已驗證程式與尚未執行的 production acceptance。

## 9. 驗證與完成條件

- pytest table/property tests：reserve=0、超過餘額、入金、提款、disabled、Decimal precision/NaN、minimum、cell headroom；不跨幣混算。
- 同一 commitment 在不同 snapshot 下恰好扣一次；external offers/unknown coverage 無法分類時 fail-closed；cancel-before-reconcile 不提前釋放。
- PostgreSQL integration：兩個不同程序／connection 競爭同一資金，總承諾不超 spendable；跨 account/realm 不互相污染；重啟仍保留 pending commitments。
- Fault injection：intent commit 前後、venue call 前後、ACK persist 前後中斷；UNKNOWN 保持阻擋，無 automatic retry。
- permit：concurrent consumption、expiry、換 account/digest/epoch、改 amount、replay、重啟、另開 session 都不能多送一次；不足最小額不自動擴大。
- normal mode 的兩個 cells 不受 one-shot scope 限制；normal amount 可超過已結束的 canary amount，仍必須通過相同 capital/risk guards。
- promotion：只有 validated 且身份／artifact／policy 正確才可轉移；stale authorization、DB failure、changed runtime 均保持 halt。
- deployment fixture：build-once artifact 經驗證後直接啟動；moving main、不相符 platform/digest、舊 env 無法繞過。
- migration：existing fixed-cap source diff、重跑 idempotency、invalid old values、draft 不生效、legacy binary 拒絕新 policy；history/permits 不遺失。
- frontend/status 與 submit 使用相同 policy revision，顯示 available、pending、reserve、可新增額與實際阻擋原因；不暴露內部 canary env 欄位讓使用者猜值。
- commit 前依 repo 執行完整 non-integration pytest，並對相關 DB migration/concurrency 跑 PostgreSQL integration、ruff/mypy 與 schema drift 檢查。

完成代表所有 live consumers 使用單一 policy、舊配置路徑刪除、以上行為驗證通過。程式完成不等於已恢復交易；production release acceptance 另列實測結果。

## 10. 參考與適用界線

- [Google SRE — Canarying Releases](https://sre.google/workbook/canarying-releases/)：限時、部分 rollout 與明確評估。單帳戶 one-shot 是本專案的適配，沒有 control cohort，不能冒稱統計 canary comparison。
- [Google SRE — Configuration Design](https://sre.google/workbook/configuration-design/)：配置來源、驗證與可還原性；本設計將 artifact 和 policy 固定為可核對版本。
- [SEC market-access controls FAQ](https://www.sec.gov/files/faq-15c-5-risk-management-controls-bd.htm)、[FINRA Algorithmic Trading](https://www.finra.org/rules-guidance/key-topics/algorithmic-trading)：參考 pre-trade controls、變更測試與監控原則。本文不判定法規適用，也不將 all_available 模式宣稱符合固定 capital threshold 的監管要求。

這份設計選擇動態資金與獨立 release 驗證，並不宣稱是所有交易系統唯一的 best practice。
