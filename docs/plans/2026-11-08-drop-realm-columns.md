# 拿掉每列的 deployment_environment 欄與每表的 realm trigger

**Goal：** 所有 live 表（`public` 30 張＋`projection_audit.runs`，共 31 張）不再有 `deployment_environment` 欄與 `database_realm_write` trigger，`guard_database_realm()` 函式不存在；realm 只剩 `database_realm` 一列 stamp 與開機時的 `assert_database_realm` 比對。程式裡 env 值只在啟動組裝處（`apps/`）讀一次，注入需要它的幾處。封存 schema（`legacy_archive`、`release_archive`）的欄位與約束原樣保留。HKDF 綁定與對外 API 欄位 `deployment_environment` 的值完全不變；advisory lock key 改成只由 namespace 與 account 導出、不含 env，值在步驟 4 的發版變一次（D6）。程式與 schema 在同一次 `dev`→`main` 發版一起換（D3）。可驗證的終態見步驟 5 的驗收。

**上層：** 主清單在 repo 外，見本機個人脈絡檔 `AGENTS.local.md`。本 repo 沒有上層主計畫，下面的「全域約束與完成定義」暫代。

**附件：** `docs/plans/2026-11-08-drop-realm-columns/`。
- `inventory.tsv`：每個帶本欄的表的欄位、PK、UNIQUE、FK（含被引用側）、索引、CHECK、realm trigger、本體讀本欄的其他 trigger function。產生於 head `96535191e443`（步驟 2 撤掉 `db388a84678f` 後重新產生），「去向」欄是 D3 的寫法。
- `inventory_probe.py`：產生 `inventory.tsv` 的探針（把空庫 migrate 到 head 後查 `pg_catalog`）。重跑（在 `backend/`）：
  `PYTHONPATH=. uv run pytest -p tests.conftest --rootdir=. -q -p no:cacheprovider ../docs/plans/2026-11-08-drop-realm-columns/inventory_probe.py`
- `check_inventory.py`：盤點每列每欄不得空白、每張 live 表都有 column 與 trigger 列。`python3 docs/plans/2026-11-08-drop-realm-columns/check_inventory.py`（2026-11-12：`189 rows, 48 tables (31 live), 0 errors`）。

## 全域約束與完成定義（暫代主計畫）

- 值不變：`SealContext.info()`（`core/sealed_credential.py`）、API 的 `deployment_environment`（`modules/api/funding_status.py`、`backend/openapi.json`、`frontend/src/types/api.gen.ts`）的值都來自設定（prod 為 `prod`），不從資料列取。因此不重新加密、不改 API 契約。advisory lock key 不在此列：三組 key 都拿掉 env（D6），只能跟拿掉本欄的 migration 同一次發版。
- migration 只往前（ADR `docs/adr/2026-10-08-forward-only-migrations.md`），而且自己斷言資料（ADR `docs/adr/2026-10-08-migrations-assert-their-data-and-deploy-rehearses-them.md`）。例外：`db388a84678f` 只在 `dev`、沒有進過 `main` 與正式環境，步驟 2 直接刪檔（D5）。
- 封存 schema 的資料列一個 byte 都不改（manifest 的 SHA-256 涵蓋 `to_jsonb(row)`）。
- import 方向：`core`、`external`、`modules` 不得 import `apps`（`backend/pyproject.toml` 的 import-linter contract `apps-is-top`）。
- 步驟 2、3、4 都進 `dev`，在步驟 4 併入之前不發版到 `main`；三者同一次發版（D3）。套 migration 一律 `cd backend && uv run alembic upgrade head`。
- 本機不跑 e2e 與 docker 測試；標 `docker` 的測試與 Playwright 推上去由 CI 跑，CI 綠之前回報為待驗。
- 完成定義（每一步）：單元測試全過、integration 全過（CI）、`alembic check` 無 drift、`mypy`／`ruff`／import-linter 乾淨、`quality_ratchet.py` 通過（測試數變少要附 `Ratchet-Reason:`／`Removed-Test:` trailer）、改行為處附 mutation、`/code-review` 跑過。

## 前提（2026-11-12 重新核對）

- 會改到的既有部署是否在線：**在線**。prod bot 正在跑，realm 只有 `prod`，沒有真錢；只在 owner 核准的 `dev`→`main` 發版時部署。有 migration 的部署先停 webapi 與所有 writer 再 migrate，bot 與 webapi 開機都比對 schema head、不符就拒絕，所以**新舊版程式不會同時跑在同一個 schema 上**，不需要相容性窗口；**回滾＝往前修**（再發一個修正 release），不做 downgrade。（來源：`deploy/vm/ops/bfx_deploy.py` 第 34–61 行、`docs/runbooks/deploy.md` 第 110–112 行、`backend/src/bfx_funding_bot/core/schema_head.py` 第 1–8 行；2026-11-12 讀程式確認）
- 還原 drill 執行的是**目標版本**的腳本：`deploy/vm/systemd/bfx-restore-test@.service` 的 `ExecStart` 以 `/home/ubuntu/bfx-releases/%i/deploy/vm/pgbackrest/restore-drill.sh` 為 drill；落後一版的只有外層 wrapper `bfx_restore_test.py`（`/usr/local/lib/bfx-ops/current`）與開機檢查用的已部署 image。`deploy/vm/pgbackrest/ledger_digest.py` 第 551–553 行註解：開機報告 JSON 來自已部署的 image、由目標版 drill 解析、多的鍵忽略。（2026-11-12 讀程式確認；取代 2026-11-08 版「drill 比 image 落後一輪」的前提，該前提是反的）
- 同一次部署的 restore test 會在**同一個 run 裡**遇到兩種組合：先是還原副本的舊 schema（有欄）與已部署 image 的開機報告（含 env 鍵）；接著 `_rehearse`（`deploy/vm/pgbackrest/restore_drill.py` 第 666–696 行）在副本上跑候選版的 `alembic upgrade head`、重讀 bounds、跑候選 image 的開機檢查（`docs/runbooks/offsite-dr.md` 第 531–534 行），也就是新 schema（無欄）與新報告（無 env 鍵）。目標版 `ledger_digest.py` 兩種都要能處理；它的 `bounds_script`（第 190–194 行）現在直接 `SELECT … deployment_environment`，在無欄的副本上會 SQL 錯誤（由 SQL 推論，未在無欄的庫上執行）。（**已確認**，4.0，2026-11-12 讀程式與本機實驗）細節：
  - 時序（`deploy/vm/ops/bfx_deploy.py` 第 35–42 行：停 webapi 與 writer → 備份 → restore test → 正式庫才 `alembic upgrade head`）。restore test 全程用目標版的 `restore_drill.py`／`ledger_digest.py`。驗證階段（`restore_drill.py` 第 950–964 行）：副本與正式庫都還是舊 schema（有欄）；bounds 讀副本（第 950 行）；開機檢查跑 `bfx-bot:local`（第 934–936、954 行），這個 alias 在部署成功後才改指新 image（`docs/runbooks/deploy.md` 第 115 行），所以此時是已部署的上一版 image、報告含 env 鍵；digest 對副本與正式庫各串流一次並比對（第 956–964 行）。
  - 演練階段（`_rehearse`，第 666–707 行）：migration 前再讀一次 bounds（第 681 行，舊 schema）、候選 image 跑 `alembic upgrade head`（第 690–695 行）、重給 verifier grant（第 696 行）、migration 後讀 bounds（第 697 行，新 schema）、候選 image 的開機檢查對 migration 後的 bounds 解析（第 699 行，報告不含 env 鍵）。**演練階段不呼叫 `_ledger_stream`／`compare`，不比 digest**；4d 的停止條件沒有觸發。
  - 比對鍵：`parse_boot` 以 `(exchange_account_id, deployment_environment)` 比對報告的 scope 與 `bounds.scopes`（`ledger_digest.py` 第 587、596–599 行）。報告少了 env 鍵時，第 586 行的 `_known(raw_scope, _BOOT_SCOPE_KEYS)` 在第 563–565 行以 `restore_output_invalid` 拒絕（本機實驗：同一份報告拿掉該鍵即被拒，traceback 經第 586、565 行）。
  - 所以 4d 的目標版 `ledger_digest.py` 要接受：`bounds_script` 與 `digest_script` 在有欄、無欄兩種 schema 都能執行（SQL 完全不提本欄，scope 只用 `exchange_account_id`，不需要偵測 schema）；`parse_boot` 接受含 env 鍵（上一版 image）與不含（候選 image）的報告，只以 `exchange_account_id` 比對。receipt 的產生器 `deploy/vm/pgbackrest/evidence.py` 與 wrapper 不讀 scope 的 env（`rg -n deployment_environment deploy` 只命中 `ledger_digest.py`）。
  - 還原副本早於本 migration 的 drill（部署之後跑、目標標籤較舊）：已部署的新 image 的開機檢查先於 digest（第 954 行在第 956 行之前），會先以 `boot_schema_head_mismatch` 拒絕，走不到 digest 比對（由程式順序推論，未實跑；任何帶 migration 的發版都一樣，不是本計畫新增的行為）。
- env 值現在有兩條來源（2026-11-12 讀程式確認）：bot 走 `apps/config.py`（`load_config`，第 111–122 行）；webapi、operator CLI 與腳本不經它，而是由 `modules` 在執行時呼叫 `core/settings.py` 第 23 行的 `require_deployment_environment()`（呼叫處：`modules/execution/operator_commands.py` 第 175 行組 `OperatorContext`——`modules/api/account_scope.py` 第 25 行的 `ExchangeAccountContext` 就是它，API 欄位與 `operator_commands.py` 第 448 行的 `SealContext(` 都由此來；`modules/live_validation/g3.py` 第 115 行；`backend/scripts/build_public_site.py` 第 63 行；`backend/scripts/run_weekly_attribution.py` 第 39 行）。另有三處直接讀環境變數：`modules/observability/error_tracking.py` 第 105 行、`modules/observability/alerts.py` 第 309 行、`modules/live_validation/g3.py` 第 117 行。`apps/webapi.py` 目前沒有 env。
- 開機檢查在 drill 裡拿不到設定值：`deploy/vm/pgbackrest/restore_drill.py` 第 453 行把 `BFX_DEPLOYMENT_ENV` 設成空字串，`apps/restore_boot_check.py` 只有唯讀的 `DATABASE_URL`；它現在比的是每個 scope 列的本欄對 stamp（第 142、156 行），不是對設定。（2026-11-12 讀程式確認）
- 帶 env 的型別欄位與參數有兩種命名：`deployment_environment` 與 `environment`（`rg -l "\.environment\b|environment=" backend/src` 41 檔，2026-11-12）。`external/bitfinex/submit_outcomes.py` 第 208 行的 `SubmissionAttemptPayload.environment` 註明是「Immutable audit identity」。（**已確認**，4.0，2026-11-12）它不進 `payload_sha256`（第 243–251 行：只雜湊 `normalized_payload`），不進已存的 journal 內容（唯一的建構處 `modules/execution/command_gate.py` 第 200–208 行只把 `attempt_id`、`normalized_payload` 交給 journal；`as_storage_dict` 在 `backend/src`、`backend/scripts`、`deploy` 沒有呼叫者，`rg -n as_storage_dict` 只命中定義與一個測試）；它只進 `attempt_id` 沒給時的 `uuid5` 推導（第 253–267 行），而唯一的建構處一律傳 `uuid4()`，已存的 `attempt_id` 也不會被重新推導（`_ATTEMPT_ID_NAMESPACE` 只用在第 266 行）。本機實驗：給定 `attempt_id` 時換 env，`attempt_id` 與 `payload_sha256` 都不變；不給時 `attempt_id` 隨 env 變。結論：4c 拿掉這個欄位，`uuid5` 的 identity 與 `as_storage_dict` 一併拿掉 env；不列入「值不變」清單。
- `ALTER TABLE … DROP COLUMN` 的行為（2026-11-12 在本機 PG18 的 head 庫實測）：表上還掛著 `UPDATE OF deployment_environment` 的 trigger、或有 FK 引用含本欄的 key 時，`DROP COLUMN` 以「other objects depend on it」拒絕；拆掉 trigger 後，`nav_peak` 的 `DROP COLUMN` 成功並自動刪掉含本欄的 PK。所以 migration 的順序是先拆 trigger、FK 與提到本欄的 CHECK，再刪欄（含本欄的 key 與 index 隨欄消失），再以原名建窄 key。
- 封存 schema 保留本欄；`legacy_archive` 12 張表仍掛 realm trigger，必須在 `DROP FUNCTION` 前拆掉（來源：`inventory.tsv`，`grep -c 'legacy_archive.*trigger' inventory.tsv`；已確認）。`release_archive` 沒有 realm trigger，它自己的 3 個 guard function 只比對封存表的 NEW／OLD，保留。
- 每張 live 表的本欄值只會是 stamp（prod 為 `prod`、CI 為 `ci`）：由 `96535191e443` 的斷言與 trigger 保證（ADR `docs/adr/2026-11-08-remove-the-shadow-realm.md`；已確認）。步驟 4 的 migration 再斷言一次。
- 每列欄位的來由：2026-05 時 prod 與 shadow 共用儲存，靠欄位隔離（ADR `docs/adr/2026-05-23-deployment-environment-realm-and-backend-ci-gate.md`）；現在一個庫只有一個 realm，這個理由不存在。（已確認）
- `origin/dev` 目前領先 `origin/main` 99 個 commit、含未發版的 migration（`git log --oneline origin/main..origin/dev | wc -l`，2026-11-12）：發版本來就是成批的。
- 正式環境各表列數與 migration 實際耗時：**未確認**，由部署前的 rehearsal 量（900 s 預算）。

**驗收基線：**（2026-11-12，`dev` 的 `e41f9702`）單元 `5360 passed`；integration（非 e2e、非 docker，本機）`1243 passed, 8 skipped`；CI 全套通過（PR #371，run 43062394775）。沒有既有失敗。

## 本階段依賴的不變量

| 不變量 | 依賴它的機制 |
|---|---|
| 一個資料庫只有一個 realm（stamp 一列、不可改） | `core/database_realm.py` 的 `read_database_realm`／`assert_database_realm`；`database_realm` 表的 `immutable_database_realm_*` trigger；`apps/bot.py` 開機比對 stamp 與設定；`apps/restore_boot_check.py` 讀 stamp（讀不到即 `boot_realm_mismatch`）並輸出 `realm` 鍵；`modules/simulated_venue/_internal/store_sql.py` 的 `_check_database_realm` |
| 拿掉本欄後，原本含 env 的 key 縮窄不會撞重複（因為每表只有一個值） | 步驟 4 migration 的斷言；以原名重建的窄 PK／UNIQUE／index |
| HKDF info、API 欄位、metrics／Sentry／告警標籤的 env 值由各組裝處（`apps/` 的 bot、webapi、operator CLI，與各腳本的 `main`）讀一次、以參數注入，不來自資料列，`modules` 不自己讀設定或環境變數（D4） | `core/settings.py` 的 `require_deployment_environment`（只給組裝處呼叫）；`apps/config.py`；`SealContext(` 建構處；`modules/execution/operator_commands.py`；`modules/api/funding_status.py`；`modules/observability/metrics.py`、`resource.py`、`error_tracking.py`、`alerts.py`；`modules/live_validation/g3.py`；import-linter contract `apps-is-top` |
| advisory lock key 只由 namespace 與 account 導出，不含 env；key 的變更與拿掉本欄的 migration 同一次發版（D6） | `core/xact_lock.py` 的 `derive_transaction_lock_key`；`core/writer_lock.py` 的 `derive_lock_key`；`modules/ledger/_internal/clock.py` 的 `lock_scope`／`holds_scope_lock`；trigger function `guard_trading_state_transition`；`backend/scripts/amend_capital_policy.py`；`apps/bot.py` 的 writer lock 呼叫處；`tests/core/test_lock_keys.py` |
| 封存表一個 byte 都不動 | `legacy_archive.manifest`、`archive_frozen` trigger、`release_archive` manifest |
| 舊 image 拒絕新 schema head；有 migration 的部署先停所有 writer | `apps/schema.py`、`core/schema_head.py`、`deploy/vm/ops/bfx_deploy.py` 的停機與 migration 順序 |
| 目標版 drill 能解析已部署（上一版）image 的開機報告，也能對 migration 前的還原副本算 digest | `deploy/vm/pgbackrest/ledger_digest.py` 的 `parse_boot`、`_BOOT_SCOPE_KEYS`、`_known`、`bounds.scopes`；`deploy/vm/pgbackrest/restore_drill.py`；`apps/restore_boot_check.py` 的輸出 |

## 規模（搬遷）

| | live（public＋projection_audit） | 封存（legacy_archive／release_archive） | 產生計數的位置 |
|---|---|---|---|
| 帶本欄的表 | 31（30＋1） | 12／5 | `awk -F'\t' '$3=="column"{print $1}' inventory.tsv \| sort \| uniq -c` |
| PK 含本欄 | 9 | 5／0 | `awk -F'\t' '$3=="PK"' inventory.tsv` |
| UNIQUE constraint | 6（其中 3 個是 FK 目標：`capital_policy_requests`、`uncertainty_resolution_requests`、`trading_control_requests`） | 1／1 | `$3=="UNIQUE"` |
| unique index（非部分） | 1（`ix_trading_state_scope_id`，FK 目標） | 2／0 | `$3=="unique-index"` |
| 部分 unique index | 5 | 5／2 | `$3=="partial-unique-index"` |
| 一般索引 | 17（其中 `ix_ledger_observation_query_scope_revision` 不重建：與同表窄 UNIQUE 欄位相同只差方向） | 6／3 | `$3=="index"` |
| composite FK（含本欄） | 6，指向 4 個目標 | 1／0 | `$3=="FK"` |
| CHECK | 2（`ck_trading_state_evidence`、`ck_sim_venue_event_realm`） | 0 | `$3=="CHECK"` |
| realm trigger | 31 | 12／0 | `$3=="trigger"` |
| 本體讀本欄的其他 trigger function | 9 個 function、16 個 trigger（`public` 8 個＋`projection_audit.guard_run`） | 0／3（保留） | `$3=="trigger-function"` |
| 程式消費端 | `backend/src` 82 檔、`backend/scripts` 3 檔、`deploy/` 1 檔、frontend 2 檔（API 欄位，值不變） | — | `rg -l deployment_environment backend/src backend/scripts deploy frontend/src` |
| 測試 | 120 檔（2026-11-12，`dev` 的 `e41f9702`） | — | `rg -l deployment_environment backend/tests \| wc -l` |

## 範圍

- 做：31 張 live 表拿掉本欄與 trigger、`guard_database_realm()` 刪除、9 個讀本欄的 trigger function 改寫；所有 key／索引／FK／CHECK 以原名改成窄版；程式、腳本、`ledger_digest.py`、`restore_boot_check.py`、`*_COLUMNS` grant 清單與 grant 測試跟著改；env 值改為組裝處注入；所有帶 env 的型別拿掉該欄位，不論欄名是 `deployment_environment` 還是 `environment`（`Scope`、`CapitalScope` 與 `modules/trading/capital.py` 裡另外三個帶 `environment` 的型別、`ResolutionScope`、`OperatorContext`、`AccountContext`、`AuditContext`；`SubmissionAttemptPayload.environment` 也拿掉：4.0 查出它不進雜湊也不進已存內容，見前提段）；讀封存表的 `modules/execution/archived_execution_history.py` 保留欄位宣告與 realm 過濾（封存表仍有本欄，而且沒有斷言過它只有 stamp 的值），過濾值改用注入的 env；開機報告不再輸出每個 scope 的 `deployment_environment`；測試、文件、ADR 收尾。
- 不做：封存 schema 的欄位與約束（manifest 雜湊）；HKDF、API 欄位的值（全域約束）；`database_realm` stamp 與開機比對（保留的唯一 realm 機制）；改既有、已發版的 migration 檔（只往前）。
- 延後：無。（2026-11-08 版的延後項「開機報告多留一版 env 鍵」建立在反的前提上，已取消，見 D3。）

## 步驟

路徑慣例：程式路徑相對於 `backend/src/bfx_funding_bot/`；測試路徑相對於 `backend/`；`backend/scripts/`、`deploy/`、`docs/` 開頭的寫全。每個檔案標（新增）（修改）（刪除）。

- [x] **1. 加窄 key、讓 guard 補值（只加不刪的 migration）**
  - 已被 D3、D5 取代（2026-11-12）：這一步的 migration `db388a84678f`、37 個 `_narrow` index 與模型宣告、guard 補值、`tests/integration/test_realm_guard_fills_stamp.py` 由步驟 2 撤掉。留下的是：同庫單一 realm 的測試 fixture 修正、盤點補列（R1）、realm 斷言的寫法（搬進步驟 4 的 migration）。已合併進 `dev`（PR #371，`ce81165d`），沒有發版。

- [x] **2. 撤掉步驟 1 的鷹架**（被擋於：無）
  - 範圍：
    - `backend/alembic/versions/db388a84678f_narrow_realm_keys.py`（刪除；head 回到 `96535191e443`）。
    - 9 個模型檔裡的 `_narrow` 宣告與註解（修改）：`modules/ledger/tables.py`、`modules/execution/safety/tables.py`、`modules/execution/capital_tables.py`、`modules/execution/uncertainty_tables.py`、`modules/execution/audit/tables.py`、`modules/accounts/rotation_tables.py`、`modules/live_validation/tables.py`、`modules/observability/tables.py`、`modules/simulated_venue/tables.py`（`rg -n "_narrow|No narrow twin" backend/src` 無結果為止）。
    - `tests/integration/test_realm_guard_fills_stamp.py`（刪除，9 個測試；`backend/quality-ratchet.json` 的 `tests_executed` 在步驟 1 沒有調高，CI 回報的測試數低於鎖定值時才調低，並附 `Ratchet-Reason:`「被 D3 取代的過渡機制的測試」與逐一的 `Removed-Test:`）；`tests/modules/execution/audit/test_tables_metadata.py`（修改：期望值拿掉 `_narrow`）；`tests/integration/test_ledger_schema_roles.py`、`tests/test_attribution_router.py` 裡提到 `db388a84678f` 的註解（修改）。
    - `docs/plans/2026-11-08-drop-realm-columns/inventory_probe.py`（修改：去向改成 D3 的寫法——含本欄的 key／index「步驟 4：隨 DROP COLUMN 消失，以原名重建窄版」，FK「步驟 4：先 DROP，刪欄後以原名重建」）與 `inventory.tsv`（重新產生，head 為 `96535191e443`）。
  - 留著不動：步驟 1 改成單一 realm 的 fixture（`tests/apps/runtime/account_test_helpers.py` 的 `seeded_realm`、各「另一個 scope 用另一個帳號」的測試）、已移除的 `other_environment` 案例。
  - 消費端：`rg -n "db388a84678f|_narrow" backend docs deploy`（只剩本計畫的歷史敘述）；`rg -n "down_revision" backend/alembic/versions | rg db388a84678f`（必須無結果：沒有 revision 接在它後面）。
  - 驗收：`cd backend && uv run alembic heads` 只有 `96535191e443`；單元全過；integration（CI）全過；`alembic check` 無 drift；`quality_ratchet.py` 通過（CI 的 `Quality ratchet` job）；`check_inventory.py` 0 errors。這一步是刪除過渡機制，沒有新行為，不另做 mutation。
  - 停止條件：有 revision 的 `down_revision` 指向 `db388a84678f`（別的分支已疊在它上面）——停下回報，不改別人的 migration。
  - 需要人做的事：owner 本機若有資料庫停在 `db388a84678f`，在拉到本步之前先在該庫跑 `cd backend && uv run alembic downgrade 96535191e443`（該 revision 的 downgrade 無損）；CI 與測試用的庫都是拋棄式，不受影響。
  - 完成後的狀態：`dev` 的 schema 與步驟 1 之前相同。

- [x] **3. env 值改為組裝處注入（純程式，D4）**（被擋於：步驟 2 已併入 `dev`；其中 lock key 的 env 來源另被 D6 擋住，見 3b）
  - 範圍（修改）：env 值只由組裝處讀一次，再以建構參數或函式參數往下傳；`modules`／`core` 不 import `apps`，也不自己讀環境變數。
    - 讀取點：bot 是 `apps/config.py`（已存在）；webapi 是 `apps/webapi.py` 的 lifespan（存進 `app.state.deployment_environment`，路由由 `modules/api/deps.py` 的 dependency 取）、operator CLI 是 `apps/operator_cli.py` 的 `resolve_operator`、腳本是各自的 `main`，這三類呼叫 `core/settings.py` 的 `require_deployment_environment()` 一次。該函式留在 `core`，只給組裝處用；`apps/config.py` 改成也呼叫它，讓驗證規則只有一份。webapi 與 CLI 讀不到合法值時存 `None`，`resolve_context` 在成員檢查之後才以 `deployment_environment_not_configured` 拒絕（維持「非成員先 404」的既有行為）。告警與 Sentry 在設定驗證之前就啟動，組裝處用同檔的 `deployment_environment_label()`（不驗證，只當標籤）傳入。
    - 改為接收注入值：`modules/execution/operator_commands.py` 第 175 行（組 `OperatorContext` 時不再自己呼叫讀取函式）、`modules/live_validation/g3.py` 第 115、117 行、`modules/observability/error_tracking.py` 第 105 行、`modules/observability/alerts.py` 第 309 行、`modules/observability/metrics.py`／`resource.py` 的標籤（原本就由 config 注入）；`core/writer_lock.py` 的呼叫處（`apps/bot.py`，原本就取自 config）、`SealContext(` 的建構處（`core/sealed_credential.py` 本身不改，HKDF info 格式不變）、`modules/api/funding_status.py` 的 API 欄位：這幾處的值改取自注入的參數，不取自 `Scope`／context 上的 env 欄位。
    - `backend/scripts/build_public_site.py` 第 63 行、`backend/scripts/run_weekly_attribution.py` 第 39 行：腳本的 `main` 就是組裝處，保留在那裡讀一次，往下用參數傳。
    - `apps/restore_boot_check.py` 不在本步（drill 裡沒有設定值可注入，見前提）；它在 4d 處理。
  - 這一步**不動**：`Scope`、`CapitalScope`、`ResolutionScope`、`OperatorContext`、`AccountContext`、`AuditContext` 與其他帶 env 欄位的型別（欄名 `deployment_environment` 或 `environment`）以及所有 SQL（欄位還在，INSERT 與 WHERE 仍需要值）；它們在步驟 4 與欄位一起拿掉。開機報告與 `apps/restore_boot_check.py` 不動。
  - 新增：`tests/core/test_realm_value_golden.py`（HKDF info 對固定輸入的 golden 值；lock key 的 golden 已在既有的 `tests/core/test_lock_keys.py`）。
  - 消費端：`rg -n "derive_transaction_lock_key|xact_lock|writer_lock" backend/src`；`rg -n "SealContext\(" backend/src backend/scripts backend/tests`；`rg -n "BFX_DEPLOYMENT_ENV|require_deployment_environment" backend/src backend/scripts`（結束時只剩 `core/settings.py` 的定義、`apps/` 與腳本 `main` 的呼叫）；`rg -n "\.deployment_environment|\benvironment\b" backend/src backend/scripts deploy`（列出每一處，標明是 SQL 用途留到步驟 4，或本步改為注入）。
  - 不能動：全域約束的值；`git diff --exit-code backend/openapi.json frontend/src/types/api.gen.ts`。
  - 驗收：單元全過；integration（CI）全過；`mypy`、`ruff`、import-linter 過；golden 測試；用既有密文解封的測試全過（`rg -ln "SealContext" backend/tests` 列出的檔）。新增 `tests/architecture/test_env_read_once.py`：`backend/src/bfx_funding_bot/modules` 與 `core`（`core/settings.py` 除外）不得出現 `BFX_DEPLOYMENT_ENV` 或呼叫 `require_deployment_environment`。mutation：在 `modules/execution/operator_commands.py` 加回對讀取函式的呼叫，該檢查要失敗；`SealContext` 建構處把 env 改成空字串，解封測試要失敗；API 的 env 欄改成固定 `"x"`，funding status 測試要失敗；lifespan 不存值，lifespan 測試要失敗。（lock key 呼叫處的 mutation 隨 3b。）
  - 停止條件：任何 golden 值變了；需要 env 值的非 SQL 用途比上面列的多出一類（不是多一個呼叫處）——停下回報。
  - 完成後的狀態：純程式，可單獨退版；`dev` 仍不發版（與步驟 4 同批）。

- **3b. lock key 的 env 來源**：D6 選 H（拿掉 env）後不再是獨立步驟。`core/xact_lock.py`、`core/writer_lock.py` 與它們的呼叫處在 4b 做，trigger function 裡的 key 在 4a 做。

- [ ] **4. 程式與 schema 一起換（D3）**（被擋於：步驟 3 已併入 `dev`）。在分支 `refactor/drop-realm-columns-final` 上做，子步驟各自 commit、各有驗收，**整個分支以一個 PR 併入 `dev`**（中途的 commit 不單獨併入：模型與 SQL 互相依賴）。一個 session 做不完時在子步驟邊界交接；分支上「已完成子步驟涵蓋的模組」的測試要綠，全套要到 4f 才綠。
  - 共用不能動：封存資料列與 manifest；`database_realm` 表與它的不可改 trigger；FK／PK／UNIQUE／index 的名稱（重建後同名）；全域約束的值；`git diff --exit-code backend/openapi.json frontend/src/types/api.gen.ts`。
  - 共用停止條件：某個查詢在單一 realm 前提下語意會改變（例如跨 realm 聚合）；HKDF info 的 golden 值變了（lock key 的 golden 在 4b 依 D6 改一次，之後不得再變）；manifest 雜湊不等——停下回報。

  - [x] **4.0 查證 drill 的兩種組合（spike，只讀與本機實驗，不產出產品程式）**
    - 讀 `deploy/vm/pgbackrest/restore_drill.py`、`ledger_digest.py`、`apps/restore_boot_check.py` 與 `docs/runbooks/offsite-dr.md` 第 488–548 行與 `restore_drill.py` 的 `_rehearse`，回答並把答案寫回本計畫的前提段：(1) 部署本 release 時，還原副本在演練前後各是哪個 schema、兩次開機檢查各跑哪個 image、digest 由哪個版本的腳本算、演練階段是否比 digest；(2) `bounds.scopes` 與開機報告的 scope 以什麼鍵比對，報告少了 env 鍵時哪一行會拒絕；(3) 所以 4d 要讓目標版 `ledger_digest.py` 接受哪幾種輸入；(4) `SubmissionAttemptPayload.environment` 是否進入雜湊、冪等鍵或已存的 journal 內容（`rg -n "SubmissionAttemptPayload" backend/src`，沿著它被序列化與雜湊的地方讀）。
    - 驗收：前提段兩條「未確認」改成「已確認」並附 `file:line`；4d 的範圍與 `SubmissionAttemptPayload.environment` 的去留依答案定稿。答案與目前的推論不同時，照「執行中改變前提」重寫 4d 再繼續。

  - [x] **4a. migration 與模型**（`c90ffc6b`；revision `545b2877e9bd`）
    - `backend/alembic/versions/<rev>_drop_realm_columns.py`（新增），順序：(1) 斷言每張掛 realm trigger 的 live 表本欄只有 stamp 的值，未 stamp 的庫有列另以「未 stamp」訊息拒絕（寫法取自已刪的 `db388a84678f`：從 `pg_trigger` 推導、排除兩個封存 schema、單一 statement）；(2) `DROP TRIGGER database_realm_write` 於 31 張 live 表與 12 張 `legacy_archive` 表（只拆 trigger，不碰列；`archive_frozen` 不動）；(3) `DROP` 6 個 composite FK 與 2 個 CHECK；(4) 9 個讀本欄的 live trigger function 各以 `CREATE OR REPLACE` 改成本體不讀本欄（跨表比對 scope 的改成只比 `exchange_account_id`；`guard_trading_state_transition` 的 advisory lock key 改成 `'bfx-trading-state:' || account`，D6）；(5) `DROP COLUMN deployment_environment` 於 31 張 live 表（含本欄的 PK／UNIQUE／index 與欄位層級的 grant 隨欄消失）；(6) 照 `inventory.tsv` 以**原名**建窄 PK、UNIQUE、部分 unique index、一般索引（`ix_ledger_observation_query_scope_revision` 不重建）；`ck_trading_state_evidence` 同名重建去掉本欄條件；`ck_sim_venue_event_realm` 不重建（sim venue 只用 ci 庫，由 `modules/simulated_venue/_internal/store_sql.py` 的 `_check_database_realm` 守住）；(7) 6 個 FK 以原名、窄欄位重建；(8) `DROP FUNCTION guard_database_realm()`；(9) 末尾斷言：非封存 schema 的 `information_schema.columns` 找不到本欄、`pg_trigger` 找不到 `database_realm_write`、`to_regprocedure('public.guard_database_realm()')` 為 NULL、非封存 schema 的 `pg_proc.prosrc` 沒有 function 提到本欄、封存表 manifest 雜湊重算不變。`downgrade()` raise（不可逆）。CI 的 `Migration lint (squawk)` job 會 lint 這支實際執行的 SQL：`DROP COLUMN`、`ADD PRIMARY KEY`／`UNIQUE`、`ADD FOREIGN KEY`、非 `CONCURRENTLY` 的 `CREATE INDEX` 各自在語句前加 `-- squawk-ignore <rule>`，理由寫在 migration 的 docstring（部署時 writer 全停、revision 的交易內不能 `CONCURRENTLY`，先例 `a9d2c4e6f813`、`96535191e443`）；FK 以單一 `ADD CONSTRAINT` 重建，不拆 `NOT VALID`／`VALIDATE`（同一交易內拆開沒有效果，squawk 也照樣標）。
    - ORM 模型（修改）：拿掉本欄，key 與 index 改成窄版（步驟 2 列的 9 個模型檔，加上其餘帶本欄的模型：`rg -ln deployment_environment backend/src -g '*tables.py'`）；四個 `REQUEST_COLUMNS`（`modules/execution/uncertainty_tables.py`、`modules/execution/capital_tables.py`、`modules/execution/safety/tables.py`、`modules/accounts/rotation_tables.py`）拿掉本欄。
    - `tests/integration/test_drop_realm_columns.py`（新增）；`tests/modules/execution/test_operator_request_schema.py`（修改：拿模型的 `REQUEST_COLUMNS` 比對最新一支 migration 的欄位常數）。
    - 驗收：新增測試：(1) head 上 live 表無本欄、無 realm trigger、函式不存在、`prosrc` 無本欄；(2) 窄 FK 仍擋下指向不存在 request 的列；(3) 封存 manifest 重算等於 migration 前；(4) 窄 PK 擋重複；(5) 9 個改寫過的 function 各有一條寫入路徑的測試仍擋下原本擋的情況（跨 account 的 scope 不一致、非法狀態轉移），合法寫入通過；(6) 植入第二個 realm 的列時 migration 拒絕、schema 留在原 revision。`alembic check` 無 drift；CI 的 `Migration lint (squawk)` job 綠。mutation（在複本跑）：拿掉一張表的 `DROP COLUMN`，(1) 失敗；少改寫一個 function，(1) 的 `prosrc` 斷言失敗；把一個 FK 改成不重建，(2) 失敗；對 `legacy_archive` 表做會改列的動作，(3) 失敗；拿掉斷言，(6) 失敗。
    - 停止條件：任何 key 重建時撞重複——停下回報，不改斷言。
    - 執行時的調整（2026-11-12）：`modules/ledger/tables.py` 的 `_SCOPE_COLUMNS` 改成只有 `exchange_account_id`，從 4b 提前到 4a（模型 import 時 `_scope_parents()` 就用它推導，不改就無法載入；scope 根表前後都是同樣 9 張，實測）。`ix_ledger_observation_query_scope_revision` 的模型宣告拿掉，`ledger/read/basis.py`、`read/reads.py`、`_internal/capital_reader.py` 的 docstring 改指窄 UNIQUE 反向掃描。`downgrade()` raise 之後，唯一從 head 往下降的測試 `tests/integration/test_candle_revisions_unique_migration.py` 改成用 `template_at` 往上建（其餘降版測試本來就從 `LAST_REVERSIBLE_REVISION` 開始）。migration 裡的封存 digest 用逐列 sha256 再雜湊（不把整表 JSON 串成一個 text，避開 1 GB 上限）。

  - [ ] **4b. ledger 與 capital：SQL、`Scope`、`CapitalScope`、`ResolutionScope`**
    - 範圍（修改）：`modules/ledger/__init__.py` 的 `Scope`、`modules/trading/capital.py` 的 `CapitalScope`、`modules/execution/uncertainty_requests.py` 的 `ResolutionScope`（欄名 `environment`）、`modules/trading/capital.py` 裡其餘帶 `environment` 的型別拿掉 env 欄位與建構參數；`modules/ledger/read/attempts.py` 第 60 行等以 `environment=` 傳遞的參數；`modules/ledger/tables.py` 的 `_SCOPE_COLUMNS`／`_scope_parents`、`modules/ledger/_internal/*.py`、`modules/ledger/read/*.py`（含 `_ATTEMPT_COLUMNS`）、`modules/ledger/wiring.py` 的 SQL 與組裝；`ON CONFLICT` target 改成窄 key。
    - lock key 拿掉 env（D6）：`core/xact_lock.py` 的 `derive_transaction_lock_key` 與 `core/writer_lock.py` 的 `derive_lock_key` 拿掉 env 參數，key＝namespace＋account；`modules/ledger/_internal/clock.py` 的 `lock_scope`／`holds_scope_lock`、`apps/bot.py` 的 writer lock 呼叫處、`backend/scripts/amend_capital_policy.py`（`--environment` 不再用來算 key）跟著改；`tests/core/test_lock_keys.py` 的 golden 換成新值；`backend/ARCHITECTURE.md` 的 key 公式在 4e 改。消費端：`rg -n "derive_transaction_lock_key|derive_lock_key|lock_scope\(|holds_scope_lock\(" backend/src backend/scripts backend/tests deploy`。這段不能早於 4a 的 migration 發版（新舊版程式會取不同的 key）。
    - 消費端：`rg -n "Scope\(|CapitalScope\(|ResolutionScope\(" backend/src backend/scripts backend/tests deploy`；`rg -n "\benvironment\b" backend/src/bfx_funding_bot/modules/ledger backend/src/bfx_funding_bot/modules/trading`；`rg -n "ON CONFLICT|on_conflict_do|index_elements" backend/src/bfx_funding_bot/modules/ledger`。
    - 驗收：`tests/modules/ledger`、`tests/integration/test_ledger_*.py` 全過；每個改了 conflict target 的 upsert 有 conflict 分支的 integration 測試。mutation：把一個 `ON CONFLICT` target 改成不存在的欄位組合，它的測試失敗。lock key：`tests/core/test_lock_keys.py` 的新 golden 通過，兩個不同 account 的 key 不同；一條 integration 測試證明舊版帶本欄的寫入 SQL 在 head schema 上失敗（D6 的安全性靠這一點，目前是推論）。mutation：推導式改回帶 env，golden 測試失敗。

  - [ ] **4c. execution、accounts、api、observability、live_validation：SQL 與 context 型別**
    - 範圍（修改）：`modules/execution/operator_commands.py` 的 `OperatorContext`、`modules/execution/protocols.py` 的 `AccountContext`、`modules/execution/audit/model.py` 的 `AuditContext` 拿掉 env 欄位；以 `environment` 為名的建構參數與欄位（`operator_request_worker.py`、`capital_policy_control.py`、`credential_rotation.py`、`managed_cancel.py`、`deployment_input.py`、`trading_state_store.py`、`modules/api/uncertainties.py`；完整清單以 `rg -n "\benvironment\b" backend/src` 為準）；`external/bitfinex/submit_outcomes.py` 的 `SubmissionAttemptPayload.environment`（拿掉欄位；`__post_init__` 的非空檢查、`uuid5` identity 與 `as_storage_dict` 裡的 env 一併拿掉；`modules/execution/command_gate.py` 第 204 行的建構參數與 `tests/external/bitfinex/test_submit_outcomes.py` 跟著改，4.0 已確認沒有已存的值依賴它）；`modules/execution/archived_execution_history.py`（修改：欄位宣告與過濾保留，過濾值改用注入的 env）；`modules/execution/safety/*_store.py`、`operator_requests.py`、`command_gate.py`、`trading_control.py`、`trading_status.py`、`uncertainty_requests.py`、`modules/execution/diagnostics/sink.py`、`modules/accounts/*`、`modules/api/*`、`modules/live_validation/*.py`、`modules/observability/bot_runs.py`、`modules/simulated_venue/*` 的 SQL；`apps/bot.py`、`apps/runtime/daemon.py` 的組裝。
    - 步驟 3 留下的暫時重複（`/code-review` 2026-11-12，記錄待本步收掉）：`request_credential_rotation` 的 seal 用注入的 `deployment_environment`，同一個函式寫進請求列與查 recipient 仍用 `context.deployment_environment`，兩者沒有互相檢查；本步拿掉 context 的欄位後只剩注入值一個來源。
    - 消費端：`rg -n "OperatorContext\(|AccountContext\(|AuditContext\(" backend/src backend/scripts backend/tests`；`rg -n "deployment_environment|\benvironment\b" backend/src`（結束時只剩允許清單，見 4f）。
    - 驗收：對應模組的單元與 integration 測試全過；grant 與請求欄位測試在新欄位集合下通過：`tests/integration/test_operator_privilege_allowlist.py`、`test_capital_policy_request_roles.py`、`test_credential_rotation_roles.py`、`test_webapi_privilege_allowlist.py`、`test_web_api_high_risk_writes.py`、`test_uncertainty_resolution_outbox.py`。mutation：把一個 `REQUEST_COLUMNS` 改回含本欄，對應 grant 測試失敗。

  - [ ] **4d. drill、開機報告與腳本**（被擋於：無；4.0 已完成，推論與查證結果一致，範圍不必重寫）
    - 範圍（修改；4.0 定稿）：
      - `deploy/vm/pgbackrest/ledger_digest.py`：scope 只以 `exchange_account_id` 推導，SQL 完全不提本欄、不偵測 schema，有欄（驗證階段的還原副本與正式庫、演練前）與沒欄（演練後）兩種 schema 跑同一份腳本。`scope` 列從 9 欄變 8 欄，`ScopeBounds.environment` 拿掉；`_ENVIRONMENT` 這個 regex 留著驗 `boot["realm"]`（第 579 行）。帶 env 的位置：第 81 行 `_SCOPE` join、第 168 行 `ScopeBounds.evidence()`（receipt 的 scope 不再輸出 `deployment_environment`；消費端 `deploy/vm/ops/bfx_restore_test.py` 第 79 行只數 `scopes` 長度）、第 190–194 行 `bounds_script`、第 227 行、第 238–239 行 orphans 檢查、第 288 行 `_ENVIRONMENT`、第 406 行 `#clock`、第 488–491 行 clock key；`_BOOT_SCOPE_KEYS` 不再要求 env，報告裡多的鍵忽略。
      - `apps/restore_boot_check.py`：`_scopes` 只取 `exchange_account_id`；刪掉逐 scope 的 env 比對（第 156 行），`boot_realm_mismatch` 只剩 `read_database_realm` 讀不到 stamp 的情況；輸出的 `realm` 鍵照舊，不再輸出每個 scope 的 env。
      - `backend/scripts/run_weekly_attribution.py`、`backend/scripts/build_public_site.py`、`backend/scripts/capital_mutation_gate.py` 的 SQL。
      - 既有測試（修改）：`tests/scripts/test_offsite_dr_ledger.py`、`tests/scripts/test_offsite_dr_restore.py`、`tests/integration/test_ledger_restore_verification.py`。
    - 新增：`tests/scripts/test_ledger_digest_schema_skew.py`（有欄、沒欄兩種 schema 產出同樣的 scope 集合；`bounds_script` 在無欄 schema 上可執行；同一個 run 先有欄再無欄的兩階段）與 `tests/scripts/test_restore_boot_report_skew.py`（新版 `parse_boot` 解析含 env 鍵的舊報告與不含的新報告都通過；舊報告樣本照 `git show 89b06dc4:backend/src/bfx_funding_bot/apps/restore_boot_check.py` 的輸出格式，寫成測試裡的固定 JSON）。
    - 消費端：`rg -n "_BOOT_SCOPE_KEYS|parse_boot|bounds\.scopes" deploy backend`；`rg -n deployment_environment deploy backend/scripts`。
    - 驗收：上列兩支測試；`tests/scripts` 全過。mutation：digest 的 scope 推導改回含本欄，無欄版失敗；`_BOOT_SCOPE_KEYS` 加回 env，新報告的解析失敗。
    - 完成後的狀態：備份標籤早於本 migration 的還原 drill，已部署的新 image 的開機檢查先以 `boot_schema_head_mismatch` 拒絕（開機檢查排在 digest 之前；推論，見前提段），屬設計內；4e 把這條寫進 `docs/runbooks/offsite-dr.md`（寫之前先核對 runbook 對 `a6c7e8f9b0d1` 先例的現有說法）。4.0 已確認演練階段不比 digest，原停止條件沒有觸發。

  - [ ] **4e. 其餘測試與文件**
    - 範圍：`backend/tests/` 其餘帶本欄的測試檔（修改；只為多 realm 植入而存在的測試刪除，列在 `Removed-Test:` trailer、`backend/quality-ratchet.json` 視需要調低）；`tests/pg_templates.py` 的 `DISABLE_REALM_TRIGGERS_SQL`／`disable_realm_triggers`（刪除）；`tests/apps/runtime/account_test_helpers.py` 的 `seeded_realm` 與 `environment` 參數（修改：不再需要時拿掉）；`backend/ARCHITECTURE.md`（修改：schema、ledger scope、invariants）；`docs/runbooks/offsite-dr.md`（修改：drill 相容說明）、`docs/runbooks/fresh-host-setup.md`（修改：trigger 描述拿掉，stamp 仍是一次性步驟）；`docs/adr/2026-11-08-drop-realm-columns.md`（新增：D0、D3、D4、D5、D6 的結論與理由）、`docs/adr/2026-05-23-deployment-environment-realm-and-backend-ci-gate.md`（修改：標註被取代的部分）。
    - 消費端：`rg -l deployment_environment backend/tests`；`rg -n "deployment_environment|database_realm_write|guard_database_realm" backend/ARCHITECTURE.md docs`。
    - `tests/integration/test_database_realm.py`：留下在舊 revision `a7c3e9f1b2d4` 上運作的升降版測試與 stamp 不可改的測試；在 head 上掃「每個帶本欄的表都有 trigger」的測試改成斷言 head 沒有該 trigger 與函式（或併入 4a 的測試後刪除，列入 `Removed-Test:`）。`tests/integration/test_shadow_realm_removal_migration.py`、`test_legacy_archive_schema.py` 在舊 revision 上運作，照舊。
    - 不能動：仍有意義的 realm 測試（stamp 不可改、開機比對、sim venue 拒非 ci 庫）。
    - 停止條件：某個要刪的測試其實守著與 realm 無關的行為——改寫不刪，回報。

  - [ ] **4f. 全套驗收與併入**
    - 新增 `tests/architecture/test_no_realm_column_sql.py`：`backend/src`、`backend/scripts`、`deploy` 裡 `deployment_environment` 只能出現在允許清單——`apps/config.py` 與注入點的參數名、API 欄位（`modules/api/funding_status.py` 與 schema）、`core/sealed_credential.py` 的 HKDF info、metrics／resource 標籤、`ledger_digest.py` 對有欄舊 schema 的處理、`modules/execution/archived_execution_history.py`（封存表的欄位與過濾）。型別檢查用 `dataclasses.fields`（或該型別實際的欄位列舉）：`Scope`、`CapitalScope` 與 `modules/trading/capital.py` 的其餘 scope 型別、`ResolutionScope`、`OperatorContext`、`AccountContext`、`AuditContext` 的欄名不得含 `deployment_environment`、`environment`、`env`。
    - 驗收：單元全過；integration（CI）全過；`alembic check`、`mypy`、`ruff`、import-linter、`quality_ratchet.py` 通過；`check_inventory.py` 0 errors；`rg -n "guard_database_realm|database_realm_write" backend/src deploy` 無結果；`rg -n "DISABLE_REALM_TRIGGERS|disable_realm_triggers" backend` 無結果（`backend/tests` 裡斷言 trigger 與函式不存在的測試、舊 revision 的 migration 測試可以提到這兩個名字）；design-review（對整個分支的 diff）與 `/code-review` 跑過。mutation：在某個 store 的 INSERT 加回本欄，允許清單檢查失敗；在 `Scope` 加回 `deployment_environment` 欄位、在 `CapitalScope` 加回 `environment` 欄位，型別檢查各自失敗。
    - PR 內文附 `Size-Justification:`（模型、SQL 與 migration 互相依賴，拆開的中間狀態測試不會綠）。
  - 需要人做的事（步驟 4 整體）：owner 核准含步驟 2、3、4 的 `dev`→`main` 發版；發版後驗收。
  - 完成後的狀態：回滾＝往前修。新程式在 migrate 後不健康時，bot 停到修正版發出為止（沒有真錢，代價是停機時間）。

- [ ] **5. 收尾**：完成定義逐項核對（每步的 CI run 連結與發版後驗收結果填進進度表）；長期守住的是 4f 的允許清單檢查、4d 的兩支 skew 測試與 4a 的 head 斷言測試（都已在 CI），`check_inventory.py` 是一次性檢查、留在附件不接 CI；需要人做的事列給 owner。驗收：`python3 docs/plans/2026-11-08-drop-realm-columns/check_inventory.py` 0 errors；`rg -n "guard_database_realm" backend/src deploy` 無結果；進度表每列有 commit、CI 結果；發版後驗收結果已填。

### 發版後驗收（一次：含步驟 2、3、4 的發版）

migration rehearsal 與還原 drill 只在 owner 核准的 `dev`→`main` 發版、bfx-deploy 部署時於正式主機跑，CI 跑不到。
- 誰觸發：owner 核准並合併發版 PR；bfx-deploy timer 部署時自己跑 restore test（`deploy/vm/systemd/bfx-restore-test@.service`、`bfx-restore-test.timer`）。
- 在哪裡看：`journalctl -u 'bfx-restore-test@*'` 與 `journalctl -u bfx-deploy`；部署時的 restore test 的 receipt 是 `$HOME/bfx/dr-evidence/restore-ledger.json`（`docs/runbooks/offsite-dr.md` 第 544–546 行；同目錄的 `restore.json` 是 acceptance drill 的，部署不會更新它，不要看那一份）。
- 通過條件：`restore-ledger.json` 的 `restore_test: true`、`measured: true`，`rehearsal.revision` 等於發版的 commit、演練後的 heads 等於新 revision、`rehearsal` 的秒數小於 900（`offsite-dr.md` 第 537–538 行）；沒有 `migration_rehearsal_failed`、`restore_output_invalid`、`boot_schema_head_mismatch`、`ledger_bound_invalid`；deploy ledger 的最後一列是成功；bot 與 webapi 以新 image 起來且健康。之後若 drill 目標早於該 migration 而回報 `boot_schema_head_mismatch`（或 `ledger_digest_mismatch`），屬設計內情況，記下 target 標籤。
- 進度表中，這一項在發版前記為「未跑－待發版」。

## 決定

- **D0 做不做、值要不要變**（已決 2026-11-08，Will）
  - 結論：做；HKDF、API 值保持不變。lock key 的部分於 2026-11-12 由 D6 修訂（拿掉 env）。ADR：4e 寫 `docs/adr/2026-11-08-drop-realm-columns.md`。
- **D1 每步要不要各自能單獨發版**（已決 2026-11-08，Will：A；**2026-11-12 被 D3 取代**）
- **D2 env 值留在哪裡**（已決 2026-11-08，Will：B 使用處直接讀 settings；**2026-11-12 被 D4 取代**）
- **D3 分段方式**（已決 2026-11-12，Will；擋住步驟：無）
  - A：expand/contract，分五次發版（先加窄 key、程式切過去、最後刪欄）。前提是新舊版程式會同時跑在同一個 schema 上；本 repo 的部署先停所有 writer、舊 image 拒絕新 schema，前提不成立。代價是一批只為過渡存在的機制（窄 index、guard 補值、跨版本相容測試、多留一版的開機報告鍵）。
  - B：程式與 schema 同一次發版，一支 migration 在部署的停機窗內改完。失效條件：開始放真錢且停機有金錢成本；或 rehearsal 超過 900 s。
  - 結論：B。2026-11-08 版的延後項與「步驟 3 部署前由 owner 確認主機 ops 版本」的關卡一併取消（它們依據的前提是反的，見前提段）。
- **D4 env 值留在哪裡**（已決 2026-11-12，Will；擋住步驟：無）
  - A：`Scope` 與各 context 保留 env 欄位（暗示每個 scope 可以不同，與一庫一 realm 矛盾）。
  - B：使用處直接讀設定（`modules` 讀 `apps/config.py` 違反 import-linter 的 `apps-is-top`；改讀別處就成了散落的全域讀取）。
  - C：在 `apps/` 的組裝處讀一次，以建構參數注入需要它的幾處。
  - 結論：C。
- **D5 已合併的步驟 1 怎麼處理**（已決 2026-11-12，Will；擋住步驟：無）
  - A：留著 `db388a84678f`，由步驟 4 的 migration 一起拆掉（歷史裡永久多一支建了又拆的 migration 與相依它的測試）。
  - B：再加一支往前的 migration 撤掉窄 index 與 guard 補值（守住「不改既有 migration」，但那條規則的理由是正式環境已套用，這支沒有；結果是兩支互相抵銷的 migration）。
  - C：直接刪檔。失效條件：有不能 downgrade 的長存資料庫停在這個 revision。
  - 結論：C。撤掉窄 index、guard 補值、`_narrow` 宣告與補值測試，migration `db388a84678f` 直接刪檔（只在 `dev`，沒進過 `main` 與正式環境）；留下單一 realm 的 fixture 修正、realm 斷言的寫法與盤點補列。
- **R1 盤點補列：本體讀本欄的其他 trigger function**（2026-11-11，步驟 1 執行時由 head 資料庫的 `pg_proc.prosrc` 查出；不是取捨：不改寫就不能刪欄）
  - `inventory_probe.py` 有 `trigger-function` 種類；改寫排在 4a。
- **R2 兩個 realm 同庫的測試 fixture 在步驟 1 改成單一 realm**（2026-11-11）
  - 這些修正留著（終態也需要）。原本記下的代價「步驟 1 到 2a 之間 realm 過濾沒有測試」在 D3 之下不再是跨發版的缺口：過濾本身在同一次發版的 4b、4c 拿掉。

- **D6 `Scope` 沒有 env 之後，advisory lock key 裡的 env 從哪裡來**（已決 2026-11-12，Will：選 H，三組 key 都拿掉 env；2026-11-12 執行步驟 3 時發現）
  - 結論：H。修訂 D0 的 lock key 部分；在 4a（trigger function 的 key）與 4b（兩個推導函式與呼叫處）做，與 migration 同一次發版。ADR 併入 4e 的 `docs/adr/2026-11-08-drop-realm-columns.md`。
  - 背景：key 含 env 是 2026-05 prod 與 shadow 同庫時為了分開同帳號的兩個 realm；現在一庫一 realm（不變量第一列），advisory lock 本來就以資料庫為界，env 在 key 裡是常數。帶 env 的 key 有三組：`core/xact_lock.py`（scope 交易鎖）、`core/writer_lock.py`（bot 的 session 鎖）、trigger function `guard_trading_state_transition` 的 `'bfx-trading-state:' || account || ':' || NEW.deployment_environment`（`backend/alembic/versions/5b1e7c9d2a40_two_state_trading_control.py` 第 54–55、226–227 行；4a 本來就要把它改成不讀本欄，所以這一組的 key 無論如何會變或要另找來源）。
  - H：三組 key 都拿掉 env（key＝namespace＋account）。不需要把值送進 ledger 內部。key 的值變一次；`tests/core/test_lock_keys.py` 的 golden、`backend/ARCHITECTURE.md` 的公式、本計畫的 Goal／全域約束／不變量第三列／步驟 3 與 4 的停止條件裡「lock key 值不變」要一起改，等於修訂 D0 的 lock key 部分（HKDF info 與 API 欄位不變）。失效條件：新舊版程式在同一個庫上同時完成寫入（各取不同的 key，互斥失效）。部署只停 compose project `bfx-app` 的容器；operator CLI（project `bfx-operator`，只在開機時比對 schema head）與 `backend/scripts/amend_capital_policy.py`（不比對 schema head）不在其內。擋住它們的是：同一次發版的 migration 拿掉 31 張 live 表的本欄，另一版的 SQL 會失敗，`ALTER TABLE` 也會等進行中的舊交易結束（由計畫與盤點推論，未實測；4a 的 integration 測試可補一條）。所以 key 的變更必須與步驟 4 的 migration 同一次發版。附帶好處：`amend_capital_policy.py` 現在從 argv 的 `--environment` 算 key，打錯就取到不同的鎖，H 之後沒有這個可能。可逆：兩個函式與一支 migration 改回去，同樣要在有 migration、停 writer 的發版裡做。
  - A：值逐層以參數傳入（各 ledger adapter 的建構參數、17 個內部上鎖函式的參數）。key 值不變、符合 D4 的字面。`rg -c "build_(…ledger factory…)\("` 在 57 個檔有 174 個呼叫可能要多一個參數；為了一個每庫固定的常數，在資金 ledger 的呼叫鏈上做一次大的機械改動。
  - B：`core/xact_lock` 留一個組裝時設定一次的 process 值（先例：`alerts.install`）。改動小；是 process 全域狀態，入口忘了設定必須 fail closed，測試要 fixture。
  - C：把 env 綁在 SQLAlchemy session factory 上（`session.info`），`lock_scope` 從 session 取。別的 factory 建的 session 會靜默缺值。
  - E：`lock_scope` 從 `database_realm` 的 stamp 讀。key 值不變、不用傳值；每次上鎖多一次讀取或要快取，而且違反 D4「不來自資料列」。
  - 建議：H（業界做法：advisory lock key 由 namespace 與被鎖實體的身分導出，環境隔離靠分庫）。推翻條件：存在不碰任何失去本欄的表、卻能在另一版 schema 上完成資金寫入的路徑；key 的變更沒有跟步驟 4 的 migration 同一次發版；repo 外有告警或 log 解析比對 key 的數值（`deploy/`、`docs/` 內查無；本機個人脈絡檔記的 repo 外告警定義於 2026-11-12 以 `rg -i "advisory|lock.?key|pg_locks|objid|writer.?lock"` 查過，無符合）；之後又讓兩個 realm 共用一個庫。
  - 獨立審查（2026-11-12，唯讀 subagent）：findings 8（blocking 1），同意 H；blocking 的一項（原簡報把 H 的安全性寫成「部署會停掉所有 writer」，實際上有兩個取鎖的程式不在停機範圍）已改寫成上面的失效條件。

拍板順序：D0、D3、D4、D5、D6 已決。

## 進度

狀態值：未開始／進行中／程式完成待驗收／完成／已取代。發版後驗收在發版前記「未跑－待發版」。

| 步驟 | 狀態 | 已跑的驗收 | 未跑的驗收與原因 | commit |
|---|---|---|---|---|
| 1 加窄 key、guard 補值 | 已取代（D3、D5；由步驟 2 撤掉鷹架） | 本機 2026-11-12：單元 `5360 passed`；integration（非 e2e、非 docker）`1243 passed, 8 skipped`；mutation 5 個都被抓到；CI 全套通過（PR #371，run 43062394775） | 不發版 | `f54226c8`、`cd821b45`，合併 `ce81165d` |
| 2 撤掉步驟 1 的鷹架 | 完成 | 本機 2026-11-12：`alembic heads` 只有 `96535191e443`；9 個模型檔與 `test_tables_metadata.py` 對 `89b06dc4` 無差異（`git diff --stat 89b06dc4`）；單元 `5360 passed`；受影響的 integration（`test_database_realm.py` 含 `alembic check`、`test_ledger_schema_roles.py`、`test_attribution_router.py`、`test_tables_metadata.py`）`60 passed`；`ruff`、`mypy`、import-linter（26 kept）乾淨；`check_inventory.py` `189 rows, 48 tables (31 live), 0 errors`，盤點的物件欄（前 5 欄）與重新產生前相同；消費端 `rg` 無結果；red proof（副本）：模型加回一個 `_narrow` index，`test_alembic_check_is_clean_and_the_downgrade_round_trips` 轉紅；CI 全套通過，含 integration 與 `Quality ratchet`（`tests_executed` 鎖定值不必調低；PR #372，run 43070624106） | 不發版（與步驟 3、4 同批） | `b379b009`、`8bde5154`，合併 `ad3993a3` |
| 3 env 值改為組裝處注入 | 完成（lock key 的部分移到 3b） | 本機 2026-11-12：單元 `5369 passed`；受影響的 integration 與 router 測試 `141 passed, 1 skipped`；`ruff`、`mypy`、import-linter（26 kept）乾淨；`git diff --exit-code backend/openapi.json frontend/src/types/api.gen.ts` 無差異；mutation 4 個都被抓到（模組加回讀取、seal 處 env 改空字串、API env 改固定值、lifespan 不存值）；`/code-review`（medium）：findings 10（blocking 0），修 7、記錄 1、駁回 2；CI 全套通過，含 integration 與 `Quality ratchet`（PR #373，run 43078994690；integration 第一次因安裝 PostgreSQL 的下載中斷而失敗，重跑通過） | 不發版（與步驟 4 同批） | `3f5880a5`、`74adfa76`，合併 `776388e4` |
| 3b lock key 的 env 來源 | 併入 4a、4b（D6 已決 2026-11-12：拿掉 env） | | | |
| 4.0 查證 drill 的兩種組合 | 完成（spike，無產品程式） | 2026-11-12：讀 `restore_drill.py`、`ledger_digest.py`、`restore_boot_check.py`、`bfx_deploy.py`、`submit_outcomes.py`、`command_gate.py`；本機實驗 2 個（`parse_boot` 對少了 env 鍵的報告回 `restore_output_invalid`；`SubmissionAttemptPayload` 換 env 時 `payload_sha256` 不變、給定的 `attempt_id` 不變）；答案寫回前提段 | 「`bounds_script` 在無欄 schema 上 SQL 錯誤」與「早於 migration 的副本先被開機檢查拒絕」是推論，未實跑（前者由 4d 的 skew 測試涵蓋） | `ce9d0322` |
| 4a migration 與模型 | 程式完成待驗收（CI 在 4f 的 PR 上跑） | 本機 2026-11-12：`tests/integration/test_drop_realm_columns.py` `8 passed`；`test_alembic_check.py` 無 drift；`test_candle_revisions_unique_migration.py` `3 passed`；4a 範圍的單元（`tests/modules/execution/test_operator_request_schema.py` 15/16、兩個 `test_tables_metadata.py`）通過；`ruff` 乾淨；mutation（red-proof，複本）7 個都被抓到：少刪一張表的欄、少改寫一個 function、少重建一個 FK、動到封存列、拿掉 realm 斷言、`guard_runtime_policy_head` 與 `guard_runtime_policy_revision` 各拿掉 account 條件；`/code-review`（medium）：findings 8（blocking 0），修 5、記錄 2、駁回 1。**未跑**：CI 的 `Migration lint (squawk)`（本機沒有 squawk，4f 的 PR 上跑）；`mypy src/` 101 errors／33 檔與其餘單元、integration 的 collection error 都是 4b–4d 要改的消費端（分支在 4f 前不綠，步驟 4 開頭已寫）；驗收 (5) 中 `guard_ledger_scope`、`guard_ledger_mirror`、`guard_ledger_observation_accept`、`guard_ledger_quarantine_source`、`guard_credential_rotation_request` 由既有的 `tests/integration/test_ledger_schema_roles.py`、`test_credential_rotation.py`（各自斷言這些 function 的錯誤訊息）在 head 上驗，要等 4b／4c 修好 collection 後跑；剩一條單元 `test_web_api_insert_names_only_the_granted_request_columns` 等 4b 改 `ResolutionScope`／`request_values` | 不發版（與步驟 2、3 同批） | `c90ffc6b` |
| 4b ledger 與 capital | 未開始 | | | |
| 4c execution、accounts、api 等 | 未開始 | | | |
| 4d drill、開機報告與腳本 | 未開始 | | | |
| 4e 其餘測試與文件 | 未開始 | | | |
| 4f 全套驗收與併入 | 未開始 | | 發版後驗收：未跑－待發版 | |
| 5 收尾 | 未開始 | | | |
