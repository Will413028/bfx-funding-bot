---
title: 後端拆成 control／marketdata／account-worker 三種 runtime role；模組三分只在必要條件成立時做；鎖只有 ledger 一個 owner；邊界終態 15 個 contract、0 條 ignore
date: 2026-10-19
status: active
supersedes: "[2026-09-28-backend-capability-modules-enforced-boundaries](2026-09-28-backend-capability-modules-enforced-boundaries.md) 的 D1（目標 context、命名與 venue 實作上層 port）、Amendment D3' 與 Followup 第 5、6、8 步"
amends:
  - "[2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md) D3（補上市場資料歸哪個 process）"
tags: [bfx-funding-bot, decision, architecture, refactoring]
---

# 三種 runtime role、依必要條件劃分的模組邊界、單一鎖 owner

## Context

2026-09-28 的模組重構做完 Task 1–4 與 P1–P4（PR #34–#57），下一步是 P5a。動手前回頭對照業界做法，發現原計畫漏了比模組更上游的問題：**行程怎麼切**。現況是一個 bot 行程、一個 TaskGroup 同時跑放貸、candle 錄製和 book snapshot。讀程式碼確認了三個問題：

- boot-wait 與憑證恢復期間整個 TaskGroup 不啟動，book snapshot 沒有錄；Bitfinex 沒有歷史 book，這段資料補不回來（`daemon.py:592-599`、`book_snapshot.py:3-4`）。
- candle writer 丟 FatalError 會連交易一起取消（`daemon.py:576-581`、`:720-724`）。
- per-account scope lock 的原語 `acquire_transaction_lock`（`core/writer_lock.py:54-72`）有四處直接呼叫：ledger 的 `ScopeLock`（`ledger/_internal/clock.py`），以及繞過它的 `execution/safety/trading_state.py:304`、`execution/operator_requests.py:267`、`scripts/bootstrap_simulation_db.py:111`。key 相同，所以正確，但 owner 不唯一。

另外，D3' 要求每個模組都分合約／`_internal`／`wiring`、每個跨模組呼叫都走 Protocol；逐條驗證後，很多地方這項要求的必要條件不成立（見 D3）。

約束：

- `external` 真錢運作中，每一步都要能部署；merge 進 main 就是部署，有 migration 只能 roll forward（[2026-10-08-forward-only-migrations](2026-10-08-forward-only-migrations.md)）。
- `external` Bitfinex：沒有歷史 book；public WS 每分鐘最多開 20 條連線、每條最多 25 個 channel；拒絕放在 5xx 裡、沒有 `cid`。
- `external` 外部帳戶要等 Bitfinex 同意與法律審查（2026-08-31 D7），目前只有一個帳戶。
- `inherited` bot runtime role 不能改包絡（[2026-09-26-runtime-role-writes-policy-toggle-under-trigger](2026-09-26-runtime-role-writes-policy-toggle-under-trigger.md) D1）；webapi 對 ledger 零寫入、不持有 KEK（2026-08-31 D4''、D5'）。仍成立：least privilege，撤銷條件未觸發。
- `inherited` 品質基線計畫的 9d、9e、9g 會改到同一批檔案。這是自家排程，只決定先後，不限制終態。
- `inherited` 2026-09-28 的 D2（apps 是 composition root）、D4（import-linter）、D5（純搬移和行為變更分開）。仍成立，本 ADR 不動。

## Options Considered

### 行程拓樸

- **基準. 行情錄製和交易分屬不同行程、不同故障範圍**：NautilusTrader 一個行程一個 `LiveNode`，同 venue 多 client「does not provide end-to-end account isolation」，跨行程資料走 stream（https://nautilustrader.io/docs/latest/concepts/live/ ）；Hummingbot 一個 bot 一個容器；ticker plant／feed handler 與 OMS 分開。
- **A. 三種 role（採用）**：`control`（webapi、CLI）；`marketdata` 全系統一個，錄 candle、重連補資料、book snapshot（唯一寫入者）；`account-worker` 每帳戶一個，持 writer lock 和憑證，自己訂閱決策用的 book WS。
- **B. A＋book 由 marketdata 經 Redis stream 扇出**（Nautilus 的完整形狀）：每筆真錢決策的資料多一跳、多依賴 Redis，而 9d 正在移除 Redis。
- **C. 單一行程、兩個 TaskGroup，收集工作在 boot 前啟動**：解決資料缺口，但故障仍然同一個行程；N>1 時 book snapshot 會重複。
- **D. 維持現狀**。

### 模組內部形狀

- **基準. 根套件即 API（Spring Modulith、Grzybek modular-monolith-with-ddd）；privacy 與 IoC 只用在保護不變量處**：Shopify 的 Packwerk 回顧指出全面 privacy check 與 IoC 讓程式更難懂，之後移除了 privacy check（https://shopify.engineering/a-packwerk-retrospective ）。
- **A. D3'：每個模組都三分、每個跨模組呼叫都走 Protocol**（現行）。
- **B. 三分與 Protocol 依必要條件逐模組決定（採用）**。
- **C. trading、ledger、execution 併成一個約 22k 行的模組**：一致性靠同一套件，失去邊界。

### 強制方式

- **A. 只用 import-linter 能表達的 contract，終態 15 個、0 條 ignore（採用）**。
- **B. Packwerk 式逐模組依賴白名單（grimp 寫測試）**：多擋的只有一般的下行依賴；危險的下行依賴已各有 contract；而且要新增 grimp 這個直接依賴。

## Decision

- **D1 拓樸**：採 A。marketdata 有自己的 TaskGroup、`faulthandler` watchdog、`restart: unless-stopped` 與 healthz；worker 保留 tick 的 seal 和 REST gap-fill（皆冪等），boundary candle 缺失時主動補；worker 的 `ws_data` 就緒來源改為自己 book WS 的 frame age。worker 的 candle 不依賴 marketdata：marketdata 停機時 worker 靠自己的 seal 與 REST gap-fill 繼續放貸，只有 REST 也補不到、超過 staleness budget 時才不出新訊號（fail closed）。修訂 2026-08-31 D3：常駐的 runtime role 是 control、marketdata、account worker 三種；reporting 維持批次 job（`apps/jobs`、timer），不是常駐 role。
- **D2 模組**：沿用現有名稱（不做舊 D1 的 `platform`／`venue`／`control`… 改名），venue 不再實作上層定義的 port（見 D5）；舊 D1 的「runtime 不得 import research」保留，由 D7 的 modules `layers`（research 放最上層）承接；「research 只能 import strategy、market」不保留：backtest 需要 `lending` 的 tracking model 與 `live_validation` 的 attribution 才能和 live 用同一套計算（`backtest/engine.py:14-15`、`backtest/frr_series.py:18`），而這條從未有 contract 守。per-account 核心為 `trading`（純）← `ledger` ← `execution`，execution 內部再用 `layers` 分層；`capital_amendment` 搬進 ledger；新增 `credentials`（隨 9g）與純的 `performance`。
- **D3''（supersedes D3'）**：根套件放合約、不帶 I/O，這條全面適用；以下三項只在必要條件成立時才做：
  - `wiring.py` 只准 apps import：模組需要讀 env、開連線或起 task。
  - 跨模組走 `Protocol` 注入：需要反轉方向、有多個實作，或測試必須替換 I/O（CredentialProvider、strategy、venue executor、`locked_guard` 成立；candle／stat reader 不成立）。
  - `_internal` privacy：保護不變量（journal、policy store、quarantine、strategy、sim venue 成立；一般 repository 不成立）。
- **D4 唯一寫入者**：scope lock 只經 ledger 的 `ScopeLock` 取得，上述三處繞過的呼叫改走它；鎖原語搬到 `core/xact_lock.py`，以 `protected` contract 只准 `ledger._internal.clock` import。正式環境的包絡改寫維持由 `amend_capital_policy` script（owner role，已經走 `ScopeLock`）執行，它是文件化的第二個寫入者，與 bot 用同一把鎖序列化；simulation realm 另有 `bootstrap_simulation_db` 建初始 policy。book snapshot 只有 marketdata 寫；`funding_candle_revisions` 在 worker 開始 REST gap-fill 之前改成內容有變才 append 或加唯一鍵。
- **D5 venue 合約**：`external.bitfinex` 擁有 `SubmitOutcome` 四值、分類函式、`VENUE_SUBMIT_REFUSALS_5XX` 與 4xx 允許清單、`retry`；adapter 回傳四值結果。NOT_SENT 保留兩個判定點（gate 在 authorize commit 後的重驗、adapter 在 transport 前），規則只有一條：**只有能證明沒有位元組送往 venue 時才是 NOT_SENT，transport 開始後誰都不得判 NOT_SENT。** cancel 的 bus publish 移到所有 cancel 的匯合點 `command_gate.cancel`。
- **D6 control plane 暴露面**：以 forbidden contract（source＝`apps.webapi`、`modules.api`）分兩段守：第一段只列今天就不可達的子模組，當天綠燈；第二段等拆出 `ledger.read`、`operator_requests` 與 `trading_state` 的讀寫兩端、vault 解密移入 `credentials` 之後，再禁 journal、憑證解密、Bitfinex 簽章、`core.xact_lock` 與 recorder。
- **D7 強制終態**：15 個 contract（top `layers`、modules `layers`、execution 內部 `layers`、4 個 purity（現有三個加新的 `performance-is-pure`）、`strategy-lower-only`、`simulated-venue-independent`、3 個 privacy `protected`、1 個 wiring `protected`、`control-plane-exposure`、`xact-lock-owner`），ignore 為 0；`backend/scripts` 列為第二個 root package，讓 `protected` 也約束 scripts。不新增依賴。現有 21 個 contract 的去向：top `layers` 吸收 `apps-is-top`、`core-is-leaf`、`venue-below-modules`；modules `layers`（`exhaustive = true`，所以沒列進去的模組會失敗，不會失去循環保護）吸收 `modules-acyclic`、`runtime-not-research`、`runtime-not-simulated-venue`、`trading-not-ledger`、`ledger-not-legacy`、`market-contracts-no-sibling-dependencies`；三個 privacy `protected` 吸收三個 `*-no-internal-access`；wiring `protected` 吸收三個 `*-wiring-is-top`；保留三個 purity、`strategy-lower-only`、`simulated-venue-independent`；刪除 `market-contracts-via-facade`（不保護不變量，purity 已由 `market-contracts-are-pure` 守住）。
- **D8 順序**：重構的 R 系列與品質計畫交錯：控制面守門與純搬移先行，品質計畫 9d、9e 接著做（9e 把 deployer 的服務集合改成由 compose 推導），憑證拆分併進 9g，行為變更與拓樸拆分在後，強制終態最後一次切換。

## Rationale

- **D1 選 A 而非 C**：C 只解資料缺口，市場資料與交易仍共命運；**不選 B**：book 扇出換來的是真錢決策多一跳和多依賴 Redis，觸發條件（連線開啟速率逼近上限）今天不成立。必要條件（研究錄製不受帳戶健康影響、市場資料與交易不共命運）今天就成立，所以不必等第二個 worker。代價：VM 上多一個容器，healthz、告警、runbook、deployer 都要認得它。
- **D2、D3'' 選 B 而非 A**：A 在必要條件不成立的地方只產生樣板和間接層，這正是 Packwerk 回顧描述的成本；**不選 C**：一致性來自單一鎖 owner 加序列化（Nautilus 也是元件分離、同 kernel 序列化），不需要同一個套件。改名對齊 D1 要動 225 個檔、沒有邊界價值，所以放棄。代價：每個模組要逐條判斷，判斷寫進 contract 才能機械檢查。
- **D4**：四處呼叫用同一個 key，所以收斂是結構修正而非正確性修正；用 `protected` contract 讓新的程式 import 不到鎖原語。包絡不改走 outbox：2026-09-26 已以 least privilege 決定 runtime role 改不了包絡，重開這個問題沒有新的外部理由。
- **D5 選「venue 擁有 Bitfinex 語意」而非 hexagonal `VenuePort`**：只有一個真實 venue，sim venue 在 HTTP 層模擬，port 的必要條件不成立。`venue-below-modules` 的 11 條 ignore 正是舊 D1 的矛盾造成的（要 venue 實作上層 port，又禁止 venue import 上層）；venue 擁有自己的型別後，它們才清得掉（清零路徑見 Followup）。
- **D6 分兩段而非一次到位**：第一段今天就能鎖住現在不可達的部分，不必等拆分；只靠 DB grants 不夠，同一個 image 的 control plane 不該在程式碼層級組得出寫入或解密路徑。
- **D7 選 A 而非 B**：B 的必要條件不成立，還多一個依賴。

## Expected Outcome

- book snapshot 不再因帳戶 boot-wait 或憑證恢復而中斷；marketdata 出錯時放貸照常，只有研究錄製中斷。
- webapi 的 import 可達集合不含 journal 寫入、憑證解密、Bitfinex 簽章與鎖原語。
- `lint-imports` 15 個 contract、`ignore_imports` 為 0；ledger 以外的程式無法 import 鎖原語。

## Followup

- R 系列實作計畫取代 2026-09-28 實作計畫（私下保存，不在本 repo）未執行的 P5a 以後各步：R1 control plane 守門第一段 → R2 純搬移（原 P5a–P7）→ R3 拒絕分類搬到 venue → 品質計畫 9d、9e → R7a vault 拆出 `credentials` → 9g → R4 adapter 四值介面 → R5 → R6 守門第二段 → R8 → R9a–R9d 拓樸拆分（R9d-0 先讓 deployer 容納新服務）→ R10（原 P10–P14）→ R11 強制終態。每個行為 PR 寫回歸測試、回滾方式與部署後檢查；碰真錢路徑的 PR merge 前跑一輪 simulated venue soak。
- ignore 清零路徑（現況 venue 11、acyclic 16，數字為預估，每個 PR 以實際 `lint-imports` 為準）：
  - R2 型別歸位：candle／stat wire row 與 `FundingBookLevel` 歸 venue，book 合約與 coverage 歸 market contracts，telemetry payload 歸 execution（venue 11→9、acyclic 16→9）。
  - R3 送單結果分類搬到 venue（venue 9→7）。
  - R7a vault 拆出 `credentials`，`Credentials` 從 venue import（acyclic 9→7）。
  - R4 拆 `AccountContext`（venue 只拿 `Credentials` 與 account id）、`ReservationRef`／`SubmittedOrder` 移到 gate、cancel publish 移出 adapter、`ReadyToSubmit`（送單輸入）、`FundingCancelAllResult`、`execution.errors` 與策略名稱改由 venue 自有型別或呼叫端傳入（venue 7→0）。
  - R5 observability 的 decorator 移到 `execution/middleware`、`oos_profitability`／`sortino` 移到 `performance`、readiness 移入 execution（acyclic 7→0）。
  - 任一步沒達到預估就在該 PR 說明剩下哪幾條、由哪一步接手，不得在 R11 前帶著 ignore 切換終態。
- 9g 的 OCI Vault 依賴：傾向官方 `oci` SDK，安裝前依供應鏈規則列出版本與傳遞依賴，由 owner 確認。

## Revocation Triggers

- worker lease 與 fencing epoch：出現第二個 worker 時做（FRR A/B 的 B 臂不算）。
- book 由 marketdata 扇出（選項 B）：public 連線開啟速率逼近每分鐘 20 條，或單一連線 channel 數逼近 25。
- `VenuePort`：接第二個真實 venue。
- 包絡改寫走 outbox 或 SECURITY DEFINER：tenant 可以改包絡時（2026-09-26 的撤銷條件）。
- 實體 package（uv workspace）：import 層級擋不住跨模組共用狀態。

## Amendment (2026-10-25)：R7a 的必要條件與落點

- **必要條件改寫**：9g 已上線（PR #247），KEK 改秘密檔、不上 KMS（[2026-08-31](2026-08-31-account-isolated-execution-target-architecture.md) Amendment 2026-10-22），所以 D2 的「`credentials` 隨 9g」與 D3'' 把 `CredentialProvider` 當成多實作的前提不再成立。`credentials` 仍然拆出，必要條件改為 D6：webapi（`modules/api/api_keys.py`）只需要憑證 metadata，拆開後才能用 contract 禁止 control plane 碰到解密。
- **落點**：解密、加密、輪替寫入與 key 權限政策在 `modules/credentials/vault.py`，只依賴 accounts、core、external。呼叫 Bitfinex 驗 key 的 `verify_account_credential` 與 `bootstrap_credential` 放在 `modules/execution/credential_bootstrap.py`，與 `credential_rotation` 共用 `KeyClient`。`credentials` 因此不 import `execution.protocols`，acyclic 在 R7a 就是 9→7（實測 `lint-imports`），不等 R4。metadata 的 `list_account_credentials` 回到 `accounts.exchange_accounts`。
- **D6 第二段的憑證部分提前**：`control-plane-exposure` 在 R7a 就禁 `modules.credentials`、`execution.credential_bootstrap`、`core.crypto`、`external.bitfinex.auth_rest`，依 D6 第一段「今天已不可達就當天鎖」的原則。R6 剩 journal、`core.xact_lock`、recorder 與 `ledger.read`／`operator_requests`／`trading_state` 的拆分。
- Followup 的「9g 的 OCI Vault 依賴」作廢；改用 KMS 的觸發條件見 2026-08-31 Amendment 2026-10-22。

## Related

- 本 ADR 來自 2026-10-19 的架構 brief：由未看過原計畫的 subagent 從零起草，經三輪獨立審查；brief 私下保存、不在本 repo，本 ADR 已收錄其決策與理由。
- [2026-09-28-backend-capability-modules-enforced-boundaries](2026-09-28-backend-capability-modules-enforced-boundaries.md) — 被部分取代的模組重構決策；D2、D4、D5 仍有效；舊 D3 早已由 D3' 取代，D3' 再由本 ADR 的 D3'' 取代。
- [2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md) — bridge model 與 process boundary 的上游決策。
- [2026-09-26-runtime-role-writes-policy-toggle-under-trigger](2026-09-26-runtime-role-writes-policy-toggle-under-trigger.md) — 包絡由 owner script 改寫的依據。
- Bitfinex WS 限制：https://docs.bitfinex.com/docs/requirements-and-limitations
