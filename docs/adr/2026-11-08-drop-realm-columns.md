---
title: 拿掉每列的 `deployment_environment` 欄與 realm trigger：realm 只記在資料庫的 stamp
date: 2026-11-08
status: active
tags: [bfx-funding-bot, decision, database, realm, migration]
---

# 拿掉每列的 `deployment_environment` 欄與 realm trigger

## Context

[2026-05-23 realm ADR](2026-05-23-deployment-environment-realm-and-backend-ci-gate.md) 讓每一列帶 `deployment_environment`，因為當時 prod 與 shadow 共用儲存，要靠欄位隔離。之後 realm 改成一個資料庫一個（`database_realm` 單列 stamp，migration `a7c3e9f1b2d4`），[2026-11-08 移除 shadow 的 ADR](2026-11-08-remove-the-shadow-realm.md) 把「只剩一個 realm 時還要不要每列標記」留給 owner。

現況（2026-11-12 盤點，head `96535191e443`）：31 張 live 表帶本欄，各掛一個 `database_realm_write` trigger；9 個 PK、6 個 UNIQUE、6 個 composite FK、23 個索引含本欄；另有 9 個 trigger function 讀它。每張表的本欄只會是 stamp 的那一個值。程式裡 `Scope`、各 context 型別與三組 advisory lock key 都帶 env。

**約束**：

- `external` 部署在線（prod bot 在跑，沒有真錢）。帶 migration 的部署先停 webapi 與 bot 再 migrate，bot 與 webapi 開機都比對 schema head、不符就拒絕（`deploy/vm/ops/bfx_deploy.py`、`core/schema_head.py`）：新舊版程式不會同時跑在同一個 schema 上。
- `self-imposed`（2026-11-13 更正；原本標成 `external`，寫的是「已加密的憑證以 HKDF info 綁定 env 值，改了要重新加密」）HKDF info 帶 env 的只有換 key 的密封請求，它只活一個 bot run（`core/sealed_credential.py`；bot 重啟後 `credential_rotation.py` 先以 `recipient_key_stale` 拒絕舊請求），沒有要重新加密的東西；長期保存的憑證只以帳號為 AAD（`modules/credentials/vault.py`）。對外 API 的 `deployment_environment`、`blockedScope.environment` 只有同一個 image 裡的前端在讀。兩者都可以改（D7）。
- `external` 封存 schema（`legacy_archive`、`release_archive`）的 manifest 是每列 `to_jsonb(row)` 的 SHA-256：封存表一個 byte 都不能動。
- `inherited` migration 只往前、自己斷言資料（[forward-only](2026-10-08-forward-only-migrations.md)、[migrations assert their data](2026-10-08-migrations-assert-their-data-and-deploy-rehearses-them.md)）。仍成立：理由是正式資料不能靠 downgrade 還原。
- `inherited`「每列要帶 realm」：理由（兩個 realm 同庫）已不存在，不再遵守。

## Options Considered

**D0 做不做**

- **保留每列標記。** 多一道 DB 層的防線（寫錯 realm 的列被 trigger 擋）。代價是每個 key、每條 SQL、每個 scope 型別都帶一個每庫固定的常數，而且型別暗示「同庫可以有兩個 env」，與一庫一 realm 矛盾。
- **拿掉（選用）。** realm 只剩 stamp 與 bot 開機比對。

**D3 分段方式**

- **A. expand/contract，分五次發版**（業界基準：[ParallelChange](https://martinfowler.com/bliki/ParallelChange.html)，先加窄 key、程式切過去、最後刪欄）。它解決的是新舊版程式同時跑在同一個 schema 上；本 repo 的部署不會出現這個狀態。代價是一批只為過渡存在的機制：重複的窄 index、補值的 guard、跨版本相容測試、多留一版的開機報告鍵。
- **B. 程式與 schema 同一次發版（選用）。** 一支 migration 在部署的停機窗內改完。

**D4 env 值留在哪裡**（當時 HKDF、API 欄位、metrics／Sentry／告警標籤需要它；D7 之後只剩 bot 的開機比對與遙測標籤）

- **A. `Scope` 與各 context 保留 env 欄位。** 改動最小；保留了「每個 scope 可以不同」的假象。
- **B. 使用處直接讀設定。** `modules` 讀 `apps/config.py` 違反 import-linter 的 `apps-is-top`；改讀別處就成了散落的全域讀取，兩處可能讀到不同的值。
- **C. 組裝處讀一次，以建構參數注入（選用）。**

**D5 已併入 `dev` 的過渡 migration `db388a84678f`**（D3 改選 B 之前照 A 做的第一步）

- **A. 留著，由後面的 migration 拆掉。** 歷史裡永久多一支建了又拆的 migration。
- **B. 再加一支往前的 migration 撤銷。** 守住「不改既有 migration」的字面，結果是兩支互相抵銷的 migration。
- **C. 直接刪檔（選用）。**

**D6 `Scope` 沒有 env 之後，advisory lock key 裡的 env 從哪裡來**

- **H. 三組 key 都拿掉 env，key＝namespace＋帳號（選用；業界基準）。** [PostgreSQL advisory lock](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS) 以資料庫為界，key 由被鎖實體的身分導出，環境隔離靠分庫。key 的值變一次。
- **A. 值逐層以參數傳入 ledger 內部。** key 不變；57 個檔的 174 個呼叫可能多一個參數，只為傳一個常數。
- **B. `core/xact_lock` 留一個組裝時設定一次的 process 值。** 改動小；是全域狀態，入口忘了設定必須 fail closed。
- **C. 綁在 session factory 上。** 別的 factory 建的 session 會靜默缺值。
- **E. 從 stamp 讀。** key 不變；每次上鎖多一次讀取或要快取，而且值來自資料列。

**D7 env 值還留給誰用**（2026-11-13；D0–D6 做完後，env 還進密封請求的 HKDF info、兩個 API 欄位、封存表的讀取過濾、`resolve_context` 的檢查與 `SimAccount`）

- **A. 只留 bot 的開機比對與遙測標籤（選用；業界基準）。** 環境隔離靠各環境自己的資料庫與憑證（[12-factor](https://12factor.net/config)）；環境名稱只當遙測標籤（Sentry `environment`、OpenTelemetry `deployment.environment`）；資料、API、加密不帶環境。
- **B. 每個連資料庫的入口都比對 stamp。** 約 35 支腳本要多一個 env 設定、三個 role 要多讀取權限，把要消除的概念擴散出去。
- **C. 維持 D6 做完時的狀態。** 型別與 API 繼續描述「每個 scope 一個 env」。

## Decision

- **D0**：31 張 live 表拿掉本欄與 trigger，`guard_database_realm()` 刪除，9 個讀本欄的 trigger function 改寫成只比 `exchange_account_id`；key、索引、FK、CHECK 以原名改成窄版（migration `545b2877e9bd`，不可 downgrade）。封存 schema 不動。（原本還寫「HKDF info 與 API 欄位的值不變」，被 D7 取代。）
- **D3**：B。migration 先斷言每張表的本欄只有 stamp 的值，再拆 trigger、FK、CHECK，刪欄，重建窄 key，末尾斷言終態與封存 manifest 不變。
- **D4**：C。`core/settings.py` 的讀取函式只給 `apps/` 與各腳本的 `main` 呼叫；`Scope`、`CapitalScope`、`ResolutionScope`、`OperatorContext`、`AccountContext`、`AuditContext` 沒有 env 欄位。
- **D5**：C。`db388a84678f` 只在 `dev`，沒進過 `main` 與任何長存的資料庫。
- **D6**：H。`core/writer_lock.py`、`core/xact_lock.py` 與 trigger function `guard_trading_state_transition` 的 key 都不含 env，而且與 `545b2877e9bd` 同一次發版。
- **D7**：A，與 `545b2877e9bd` 同一次發版。`BFX_DEPLOYMENT_ENV` 的讀者只剩 bot（`apps/config.py`，開機以 `assert_database_realm` 比對 stamp，並當 metrics／告警／halt gauge 的標籤）與 webapi、bot 的 Sentry 標籤；webapi 沒有這個值也能開機與服務。拿掉的：
  - 密封請求的 HKDF info 不含 env，`INFO_LABEL` 換成 `v2`，舊格式的密文一律解不開。
  - `resolve_context` 的 `deployment_environment_not_configured`、webapi 的 `app.state.deployment_environment`、operator CLI 的讀取。
  - API：funding status 與 dry run 的 `deployment_environment`、`blockedScope.environment`；webapi 對 bot 回報只比 `account_id`。
  - 封存讀取（`archived_execution_history.py`）不再以 env 過濾；`545b2877e9bd` 先斷言 `legacy_archive.event_log` 每列的本欄都等於 stamp（只讀，不改列），不符就拒絕升版。
  - `SimAccount` 沒有 realm 欄位（設定的 `require_simulated_realm` 與 store 的 `_check_database_realm` 仍在）。
  - 一次性容器（operator CLI、每週報告、公開站建置）不再收到 `BFX_DEPLOYMENT_ENV`。
  - `scripts/amend_capital_policy.py` 的 dry-run 報告多一個 `realm` 鍵（讀到的 stamp），並進 digest。

## Rationale

- **D0**：每列標記的好處是 DB 層多擋一次寫錯 realm；放棄它換來的是 scope 只有一個欄位、key 與 SQL 變窄、型別不再描述不存在的狀態。放棄之後的防線：bot 開機比對 stamp 與設定；simulated venue 開啟 store 時要求 stamp 是 `ci`。**變弱的地方**：webapi、operator CLI 與每週腳本不比對 stamp，也不再有 trigger 擋它們的寫入；它們連到哪個資料庫完全由各自的 `DATABASE_URL`（每個 role 一份秘密檔）決定。`scripts/amend_capital_policy.py` 只確認 stamp 存在，並把讀到的值放進 dry-run 報告讓 owner 在套用前看到。
- **D3**：不選基準 A，因為它要解決的並行狀態被部署流程排除了，留下的只有成本。B 的代價是回滾只能往前修：新程式在 migrate 後不健康時，bot 停到修正版發出為止；目前沒有真錢，代價是停機時間。
- **D4**：C 比 A 多改很多建構處，換來「env 只有一個來源」可以用測試鎖住（`tests/architecture/test_env_read_once.py`）。
- **D5**：「不改既有 migration」的理由是正式環境已套用；這支沒有，所以刪檔不違反它保護的東西。
- **D6**：H 讓 key 的值變一次。風險是新舊版程式各取不同的 key、互斥失效；擋住它的是同一支 migration 拿掉了本欄，另一版提到本欄的 SQL 一律失敗（`tests/integration/test_drop_realm_columns.py::test_a_statement_of_the_previous_version_cannot_run` 實測；「舊版沒有不提本欄的資金寫入路徑」是讀程式的推論）。A 不改 key 但在資金 ledger 的呼叫鏈上做大範圍機械改動；B、C、E 各自引入一個會靜默出錯的全域來源。

- **D7**：D0 留下的 env 用途都建立在「同庫可能有兩個 env」或「改了要重新加密」上，前者已不成立、後者查證後是錯的。不選 B，因為它把一個每庫固定的常數變成每個入口都要設定的東西；不選 C，因為 webapi 少了這個設定就整個 503，而它用這個值做的事（比對 bot 回報的同名鍵、過濾只有一種值的封存表）沒有保護到任何東西。代價：部署跨越時還在等待的換 key 請求解不開，operator 重送（bot 重啟後本來就會以 `recipient_key_stale` 拒絕）；封存表若真的有第二種 env 值，升版會被斷言擋下，要先決定那些列怎麼處理。

## Expected Outcome

- head 上非封存 schema 找不到本欄、`database_realm_write` 與 `guard_database_realm()`（`test_drop_realm_columns.py`）。
- 還原 drill 的同一份 SQL 在有欄與無欄的副本都能跑，並能解析上一版 image 含 env 鍵的開機報告（`tests/scripts/test_ledger_digest_schema_skew.py`、`test_restore_boot_report_skew.py`）。
- 發版時的 migration rehearsal 在 900 秒預算內（待發版時量）。

## Followup

- 發版後驗收（`restore-ledger.json` 的 rehearsal 結果、bot 與 webapi 以新 image 健康）：見 `docs/plans/2026-11-08-drop-realm-columns.md`「發版後驗收」。
- phase `shadow`→`simulated` 改名與 `OPTIMIZER_SHADOW` 移除（[移除 shadow 的 ADR](2026-11-08-remove-the-shadow-realm.md) Followup）在本項之後。

## Revocation Triggers

- 又要讓兩個 realm 共用一個資料庫 → D0、D6 與 D7 都要重議。
- 開始放真錢且部署停機有金錢成本，或 rehearsal 超過 900 秒 → 之後的大型 schema 變更回到 expand/contract（D3 的 A）。
- 出現新舊版程式會在同一個 schema 上同時寫入的部署方式 → D3、D6 的前提失效。

## Related

- 取代：[2026-05-23-deployment-environment-realm-and-backend-ci-gate](2026-05-23-deployment-environment-realm-and-backend-ci-gate.md) 的每列 `deployment_environment` 標記（見該檔 Amendment 2026-11-12）；[2026-11-08-remove-the-shadow-realm](2026-11-08-remove-the-shadow-realm.md) D2 的 `ck_sim_venue_event_realm` 關卡與「每列標記留給 owner 決定」（見該檔 Amendment 2026-11-12）。
- 計畫與盤點：`docs/plans/2026-11-08-drop-realm-columns.md`、`docs/plans/2026-11-08-drop-realm-columns/inventory.tsv`。
