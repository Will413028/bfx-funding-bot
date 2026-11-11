# 拿掉每列的 deployment_environment 欄與每表的 realm trigger

**Goal：** 所有 live 表（`public` 30 張＋`projection_audit.runs`，共 31 張）不再有 `deployment_environment` 欄與 `database_realm_write` trigger，`guard_database_realm()` 函式不存在；realm 只剩 `database_realm` 一列 stamp 與開機時的 `assert_database_realm` 比對。封存 schema（`legacy_archive`、`release_archive`）的欄位與約束原樣保留。HKDF 綁定、advisory lock key、對外 API 欄位 `deployment_environment` 的值完全不變。可驗證的終態見步驟 5 的驗收。

**上層：** 主清單在 repo 外，見本機個人脈絡檔 `AGENTS.local.md`。本 repo 沒有上層主計畫，下面的「全域約束與完成定義」暫代。

**附件：** `docs/plans/2026-11-08-drop-realm-columns/`，快照 commit `89b06dc4`（origin/dev，alembic head `96535191e443`）。
- `inventory.tsv`：每個帶本欄的表的欄位、PK、UNIQUE、FK（含被引用側）、索引、CHECK、trigger，與「去向」欄。
- `inventory_probe.py`：產生 `inventory.tsv` 的探針（把空庫 migrate 到 head 後查 `pg_catalog`）。重跑（在 `backend/`）：
  `PYTHONPATH=. uv run pytest -p tests.conftest --rootdir=. -q -p no:cacheprovider ../docs/plans/2026-11-08-drop-realm-columns/inventory_probe.py`
- `check_inventory.py`：盤點每列每欄不得空白、每張 live 表都有 column 與 trigger 列。`python3 docs/plans/2026-11-08-drop-realm-columns/check_inventory.py`（2026-11-11，head `db388a84678f`，加入 `trigger-function` 列之後：`189 rows, 48 tables (31 live), 0 errors`；2026-11-08 為 170 列；把一格「去向」清空的複本會印出 `FAIL row …`，複本放在附件目錄內、用完已刪）。

## 全域約束與完成定義（暫代主計畫）

- 值不變：`SealContext.info()`（`core/sealed_credential.py`）、`derive_transaction_lock_key`（`core/xact_lock.py`）、writer lock（`core/writer_lock.py`）、API 的 `deployment_environment`（`modules/api/funding_status.py`、`backend/openapi.json`、`frontend/src/types/api.gen.ts`）全部從 settings 取值（`apps/config.py` 的 `DeploymentEnvironment`，prod 為 `prod`），不從資料列取。因此不重新加密、不換 lock key、不改 API 契約。
- migration 只往前（ADR `docs/adr/2026-10-08-forward-only-migrations.md`），而且自己斷言資料（ADR `docs/adr/2026-10-08-migrations-assert-their-data-and-deploy-rehearses-them.md`）。
- 封存 schema 的資料列一個 byte 都不改（manifest 的 SHA-256 涵蓋 `to_jsonb(row)`）。
- 每一步各自一個 PR 進 `dev`，各自可單獨發版（D1）；套 migration 一律 `cd backend && uv run alembic upgrade head`。
- 本機不跑 e2e 與 docker 測試；標 `docker` 的測試與 Playwright 推上去由 CI 跑，CI 綠之前回報為待驗。
- 完成定義（每一步）：單元測試全過、integration 全過（CI）、`alembic check` 無 drift、`mypy`／`ruff` 乾淨、`quality_ratchet.py` 通過（測試數變少要附 `Ratchet-Reason:`／`Removed-Test:` trailer）、改行為處附 mutation、`/code-review` 跑過。

## 前提（開工時核對過，2026-11-08）

- 會改到的既有部署是否在線：**在線**。prod bot 正在跑，realm 只有 `prod`，沒有真錢；只在 owner 核准的 `dev`→`main` 發版時部署。bfx-deploy 在 migration 前同時停 webapi 與 bot，跑完 migration 再以新 image 起來；bot 開機比對 schema head，舊 image 拒絕新 schema，所以**回滾＝往前修**（再發一個修正 release），不做 downgrade。步驟 1、3 改正式 schema，各自附相容性說明；步驟 2 只改程式。（來源：repo `AGENTS.md`、`docs/runbooks/deploy.md`；已確認）
- 封存 schema 保留本欄；`legacy_archive` 12 張表仍掛 realm trigger，必須在 `DROP FUNCTION` 前拆掉（來源：`inventory.tsv`，`grep -c 'legacy_archive.*trigger' inventory.tsv`；已確認）。`release_archive` 沒有 trigger。
- shadow realm 已移除，每張 live 表的本欄值只會是 stamp（prod 為 `prod`、CI 為 `ci`）：由 `96535191e443` 的斷言與 trigger 保證（來源：ADR `docs/adr/2026-11-08-remove-the-shadow-realm.md`；已確認）。步驟 1 的 migration 仍再斷言一次。
- 正式環境備份還原 drill 用的 `deploy/vm/pgbackrest/ledger_digest.py` 是**主機上已安裝的 ops release**（`/usr/local/lib/bfx-ops/current`，「下一輪 bfx-deploy 才用新版」，`docs/runbooks/deploy.md` 第 472 行），比部署的 image 落後一輪；它的 COPY 是 `t.*`，而且 scope join 用 `(exchange_account_id, deployment_environment)`（`grep -n deployment_environment deploy/vm/pgbackrest/ledger_digest.py`）。restore gate 要求 drill、已安裝的 wrapper、部署的 image 三者一致（`docs/runbooks/offsite-dr.md` 第 488–530 行）。（已確認）
- PG18 在 `BEFORE ROW` trigger 之後才檢查 NOT NULL，所以 trigger 可以替省略的欄補值（PostgreSQL 文件 CREATE TRIGGER；預設（可推翻），步驟 1 的 integration 測試會實證）。
- 用 `ALTER TABLE … ADD PRIMARY KEY USING INDEX` 把事先建好的 unique index 換成 PK、用 `NOT VALID`＋`VALIDATE CONSTRAINT` 重建 FK，鎖表時間短（PostgreSQL 文件；預設（可推翻））。

**驗收基線：**（2026-11-08，commit `89b06dc4`，worktree `refactor/drop-realm-columns`）`cd backend && uv sync && uv run pytest -m "not integration" -n auto -q`：`5360 passed, 17 warnings, 31 subtests passed in 158.36s`，沒有既有失敗。worktree 沒有 repo root 的 `.env`，所以沒建 `backend/.env` symlink；單元測試不需要它。integration 與 docker 測試在本機沒跑，以 CI 為準。

## 本階段依賴的不變量

| 不變量 | 依賴它的機制 |
|---|---|
| 一個資料庫只有一個 realm（stamp 一列、不可改） | `core/database_realm.py` 的 `read_database_realm`／`assert_database_realm`；`database_realm` 表的 `immutable_database_realm_*` trigger；`apps/bot.py`、`apps/restore_boot_check.py` 開機比對；`modules/simulated_venue/_internal/store_sql.py` 的 `_check_database_realm` |
| 拿掉本欄後，原本含 env 的 key 縮窄不會撞重複（因為每表只有一個值） | 步驟 1 migration 的斷言；窄 unique index；步驟 3 的 PK／FK 重建 |
| lock key、HKDF info、API 欄位、開機報告與 metrics 標籤的 env 在使用處直接讀 settings，不來自資料列、也不經 `Scope`／context 傳遞（D2＝B） | `core/xact_lock.py`、`core/writer_lock.py` 的呼叫處；`SealContext(` 建構處；`modules/api/funding_status.py`；`apps/restore_boot_check.py`；`modules/observability/metrics.py`、`resource.py`；`apps/config.py` |
| 封存表一個 byte 都不動 | `legacy_archive.manifest`、`archive_frozen` trigger、`release_archive` manifest |
| 舊 image 拒絕新 schema head | `apps/schema.py`、bfx-deploy 的停機與 migration 順序 |
| 開機報告契約跨一個 release 仍可解析（主機 drill 比 image 落後一個 release） | `apps/restore_boot_check.py` 輸出的 `boot.scopes[].deployment_environment`；`deploy/vm/pgbackrest/ledger_digest.py` 的 `parse_boot`、`_BOOT_SCOPE_KEYS`、`_known`、`bounds.scopes`；`/usr/local/lib/bfx-ops/current` |

## 規模（搬遷）

| | live（public＋projection_audit） | 封存（legacy_archive／release_archive） | 產生計數的位置 |
|---|---|---|---|
| 帶本欄的表 | 31（30＋1） | 12／5 | `awk -F'\t' '$3=="column"{print $1}' inventory.tsv \| sort \| uniq -c` |
| PK 含本欄 | 9 | 5／0 | `awk -F'\t' '$3=="PK"' inventory.tsv` |
| UNIQUE constraint | 6（其中 3 個是 FK 目標：`capital_policy_requests`、`uncertainty_resolution_requests`、`trading_control_requests`） | 1／1 | `$3=="UNIQUE"` |
| unique index（非部分） | 1（`ix_trading_state_scope_id`，FK 目標） | 2／0 | `$3=="unique-index"` |
| 部分 unique index | 5 | 5／2 | `$3=="partial-unique-index"` |
| 一般索引 | 17 | 6／3 | `$3=="index"` |
| composite FK（含本欄） | 6，指向 4 個目標 | 1／0 | `$3=="FK"` |
| CHECK | 2（`ck_trading_state_evidence`、`ck_sim_venue_event_realm`） | 0 | `$3=="CHECK"` |
| realm trigger | 31 | 12／0 | `$3=="trigger"` |
| 本體讀本欄的其他 trigger function | 9 個 function、16 個 trigger（`public` 8 個＋`projection_audit.guard_run`） | 0／3（只比對封存表自己的 NEW／OLD，保留） | `$3=="trigger-function"`（2026-11-11 補盤，見決定 R1） |
| 程式消費端 | `backend/src` 82 檔、`backend/scripts` 3 檔、`deploy/` 1 檔、frontend 2 檔（API 欄位，值不變） | — | `rg -l deployment_environment backend/src backend/scripts deploy frontend/src` |
| 測試 | 118 檔 | — | `rg -l deployment_environment backend/tests \| wc -l` |

任務簡報給的「8 個 composite FK」「6 個部分 unique index」與實測不同：以 catalog 實測的 6 與 5 為準（上面的指令）。


## 範圍

- 做：31 張 live 表拿掉本欄與 trigger、`guard_database_realm()` 刪除；所有 key／索引／FK／CHECK 改成窄版；程式、腳本、`ledger_digest.py`、`restore_boot_check.py`、`*_COLUMNS` grant 清單與 grant 測試跟著改；測試、文件、ADR 收尾。
- 不做：封存 schema 的欄位與約束（manifest 雜湊）；HKDF、lock key、API 欄位的值（全域約束）；`database_realm` stamp 與開機比對（保留的唯一 realm 機制）；改既有 migration 檔（只往前）。
- 延後：`restore_boot_check` 的開機報告停止輸出每個 scope 的 `deployment_environment`，`ledger_digest.py` 的 `_BOOT_SCOPE_KEYS` 同時改成不要求它。觸發條件：步驟 3 部署之後**再完整發版一次**，而且該次部署後由 owner 在正式主機跑 `readlink /usr/local/lib/bfx-ops/current`，確認指向步驟 3 以後的 revision（舊 drill 與新 image 之間只差一個 release，見不變量「開機報告契約跨一個 release 仍可解析」）。收尾時搬回主清單。

## 步驟

路徑慣例：程式路徑相對於 `backend/src/bfx_funding_bot/`；測試路徑相對於 `backend/`；`backend/scripts/`、`deploy/`、`docs/` 開頭的寫全。每個檔案標（新增）（修改）（刪除）。

- [x] **1. 加窄 key、讓 guard 補值（只加不刪的 migration）**（被擋於：無；D1＝A 已決）
  - 範圍：
    - `backend/alembic/versions/<rev>_narrow_realm_keys.py`（新增）：(a) 斷言每張掛 realm trigger 的 live 表（沿用 `96535191e443` 的範圍，沒掛 trigger 的暫存表不擋）`deployment_environment` 只有 stamp 這一個值；未 stamp 的庫有任何列另以「未 stamp」訊息拒絕（有別的值就 `RAISE`，訊息帶表名不帶資料）；(b) `CREATE OR REPLACE FUNCTION guard_database_realm()`：`NEW.deployment_environment IS NULL` 時填入 stamp，其餘行為不變（stamp 缺→拒、值不等於 stamp→拒）；(c) 照 `inventory.tsv` 去向為「步驟 1 建窄版／窄 unique index」的 28＋9 列（`ix_ledger_observation_query_scope_revision` 不建窄版：與該表窄 unique index 欄位相同只差方向，去向改為步驟 3 刪除），各建去掉本欄的對應 index（PK 與 UNIQUE 建成 unique index，部分 unique 保留同一個 `WHERE`，暫用 `_narrow` 後綴）。新窄 index 若與既有 index 欄位完全相同就不建，在去向裡改記「步驟 3 刪除（與 X 重複）」並重跑 check。
    - ORM 模型宣告這些 index（修改）：`modules/ledger/tables.py`、`modules/execution/safety/tables.py`、`modules/execution/capital_tables.py`、`modules/execution/uncertainty_tables.py`、`modules/accounts/rotation_tables.py`、`modules/live_validation/tables.py`、`modules/observability/tables.py`（實際清單以 `inventory.tsv` 的表對到模型為準）。
    - `tests/integration/test_realm_guard_fills_stamp.py`（新增）。
    - 在同一個庫替同一個 key 種兩個 realm 的測試 fixture（修改，R2）：窄 unique index 從這一步就存在（模型的 `create_all` 與 migration 都有），這類 fixture 會撞重複，所以隨本步改成單一 realm，不等步驟 4。做法：共用的 `tests/apps/runtime/account_test_helpers.py::seed_exchange_account` 只種受測行程設定的那個 realm；「另一個 scope」改用另一個 exchange account；仍要驗 realm 不一致會被擋的測試，讓另一個 realm 的列在窄 key 上也不同；只剩「另一個 realm 可以另有一筆」這種斷言的直接拿掉。被刪的參數化案例列在 `Removed-Test:` trailer。
    - migration 的 downgrade 寫出無損反向（刪窄 index、guard 換回不補值的本體）：既有 fixture（例如 `tests/integration/test_candle_revisions_unique_migration.py`）從 head downgrade 造升級前狀態，會經過本 revision。
  - 消費端：`rg -n "guard_database_realm|database_realm_write" backend deploy`；`rg -n "ON CONFLICT|on_conflict_do" backend/src`（此步不改，確認窄 index 不影響現有 conflict target）。
  - 不能動：既有 PK／UNIQUE／FK／CHECK、欄位 NOT NULL、程式碼行為（`backend/src` 只加模型的 index 宣告）。
  - 驗收：單元全過；integration（CI）跑新增測試：(1) INSERT 省略本欄的列被補成 stamp；(2) 明寫錯的 realm 仍被拒；(3) 未 stamp 的庫仍被拒；(4) migration 在植入第二個 realm 的列時失敗；(5) 每個含本欄的 key／index 都有一個欄位、順序、唯一性與條件相同、只少本欄的 `_narrow` 版（共 37 個）；(6) head 上每個含本欄的 key／index 都找得到窄版；(7) 每張表上本體讀本欄的 BEFORE INSERT row trigger 名稱都排在 `database_realm_write` 之後（BEFORE ROW trigger 依名稱順序觸發，排在前面的會看到未補值的 NULL）。mutation：`NARROW_INDEXES` 少一列，(5) 要失敗；把 (b) 改回不補值的舊函式，(1) 要失敗；拿掉 (a) 的斷言，(4) 要失敗。`alembic check` 無 drift；`check_inventory.py` 0 errors。
  - 發版後驗收：見下方「發版後驗收」。
  - 停止條件：(a) 在 CI 或 rehearsal 發現多個 realm；或窄 unique index 在 rehearsal 撞重複——停下回報，不改斷言。
  - 需要人做的事：owner 核准含本步的 `dev`→`main` 發版；發版後驗收。
  - 完成後的狀態：可單獨發版；舊程式照寫本欄，不受影響。回滾＝往前修。

- [ ] **2. 程式不再讀寫本欄，env 從 Scope／context 移除（D2＝B）**：拆成 2a、2b、2c 三個子步驟，各自一個 PR，各自可單獨發版（D1＝A），依序做。三個子步驟共用的規則：
  - env 值只在需要它的那一處直接讀 settings（`apps/config.py` 的 `DeploymentEnvironment`）：`core/sealed_credential.py` 的 `SealContext` 建構處（HKDF info）、`core/xact_lock.py`／`core/writer_lock.py` 的 lock key 呼叫處、API（`modules/api/funding_status.py`）、`apps/restore_boot_check.py` 的輸出、`modules/observability/metrics.py`／`resource.py` 的標籤。不經由 `Scope` 或任何 context 傳遞。
  - 例外（R2-3）：由 `REQUEST_COLUMNS` 組出的 INSERT（`modules/execution/operator_requests.py` 第 70 行要求 values 恰等於 `model.REQUEST_COLUMNS`）在步驟 2 **仍帶本欄**，值從 settings 讀，guard 驗證等於 stamp；步驟 3 隨 `REQUEST_COLUMNS` 一起拿掉。
  - 共用不能動：全域約束的四個值；`git diff --exit-code backend/openapi.json frontend/src/types/api.gen.ts`；開機報告每個 scope 的 `deployment_environment` 鍵在步驟 2、3 的 release 都保留（主機 drill `deploy/vm/pgbackrest/ledger_digest.py` 第 550–587 行以 `_BOOT_SCOPE_KEYS`、`_known` 要求它，缺鍵即 `restore_output_invalid`，並以 `(account, env)` 對 `bounds.scopes`；主機 drill 比 image 落後一個 release）；`ledger_digest.py` 對有欄、沒欄兩種 schema 都要能跑。
  - 共用停止條件：任何 golden 值變了；某個查詢在單一 realm 前提下語意會改變（例如跨 realm 聚合）——停下回報。
  - 共用需要人做的事：owner 核准含該子步驟的發版；發版後驗收（見下方）。
  - 讀取投影常數（R2-1）：`modules/ledger/read/attempts.py` `_ATTEMPT_COLUMNS`（含本欄，2a 拿掉）；`modules/ledger/read/operator_reads.py` `_RESOLUTION_COLUMNS`、`modules/ledger/read/match_evidence.py` `_OFFER_COLUMNS`（常數本身不含本欄，檔內其餘用到本欄的查詢在 2a 處理）。

  - [ ] **2a. SQL 與 digest 不再用本欄**（被擋於：步驟 1 已併入 `dev`）
    - 範圍（修改）：所有 store／reader 的 `WHERE`／`JOIN`／`ON CONFLICT` 改用窄 key（conflict target 指向步驟 1 的窄 index）；INSERT 不再帶本欄（由 guard 補；`REQUEST_COLUMNS` 例外）；`modules/ledger/tables.py` 的 `_SCOPE_COLUMNS`／`_scope_parents` 改成只看 `exchange_account_id`；`modules/ledger/_internal/*.py`、`modules/ledger/read/*.py`、`modules/execution/safety/*_store.py`、`modules/live_validation/*.py`、`modules/observability/bot_runs.py`、`modules/execution/diagnostics/sink.py`；ORM 模型的本欄改成 `server_default=FetchedValue()`，`alembic check` 仍無 drift；`deploy/vm/pgbackrest/ledger_digest.py`：bounds 的 scope 只以 `exchange_account_id` 推導，`bounds.scopes` 的 env 改由還原副本的 `database_realm` stamp 補上（每個 scope 同一值），`#clock` 不再選本欄，`_BOOT_SCOPE_KEYS` **仍要求** env；`apps/restore_boot_check.py` 的 `_scopes` 只取 `exchange_account_id`，輸出的 env 取自 `read_database_realm` 讀到的 stamp，`boot_realm_mismatch` 改為比對 stamp 與 settings；腳本 `backend/scripts/run_weekly_attribution.py`、`backend/scripts/build_public_site.py`、`backend/scripts/capital_mutation_gate.py`。此時 `Scope` 等型別仍帶 env 欄位，但 SQL 不再讀它。
    - 新增：`tests/architecture/test_no_realm_column_sql.py`（允許清單檢查）；`tests/scripts/fixtures/ledger_digest_89b06dc4.py`（新增；由 `git show 89b06dc4:deploy/vm/pgbackrest/ledger_digest.py > tests/scripts/fixtures/ledger_digest_89b06dc4.py` 產生，來源 blob SHA `3b48384bc6ffe327511cb10819b1d8bc2abe7bef` 寫在檔頭註解）；`tests/scripts/test_ledger_digest_schema_skew.py`、`tests/scripts/test_restore_boot_report_skew.py`（新增；`tests/scripts/` 目錄已存在，`fixtures/` 子目錄新增）；每個改了 conflict target 的 upsert 補 integration 測試。
    - 消費端：`rg -n deployment_environment backend/src backend/scripts deploy`（快照 86 檔）；`rg -n "ON CONFLICT|on_conflict_do|index_elements" backend/src`；`rg -n "_BOOT_SCOPE_KEYS|parse_boot|bounds\.scopes" deploy backend`。
    - 驗收：單元全過；integration（CI）全過；`alembic check`、`mypy`、`ruff`、import-linter 過；(1) 允許清單檢查：SQL 與 Core 表達式裡 `deployment_environment` 只能出現在模型的伺服器填值宣告、開機報告輸出、`_BOOT_SCOPE_KEYS`，以及已知例外——由四個 `REQUEST_COLUMNS` 組出的 INSERT（`modules/execution/uncertainty_tables.py`、`modules/execution/capital_tables.py`、`modules/execution/safety/tables.py`、`modules/accounts/rotation_tables.py`），步驟 3 移除這個例外；(2) digest 對有欄與沒欄兩種 schema 產出同樣的 scope 集合；(3) 用 fixture 裡 `89b06dc4` 版的 `parse_boot` 解析 2a 的 `restore_boot_check` 輸出要通過；(4) upsert 的 conflict 分支測試。mutation：在某個 store 的 INSERT 或 WHERE 加回本欄，(1) 要失敗；把一個 `ON CONFLICT` target 改回寬 index 的欄位，它的 (4) 要失敗；開機報告拿掉 env 鍵，(3) 要失敗；digest 的 scope 推導改回含本欄，(2) 的無欄版要失敗。
    - 完成後的狀態：可單獨發版；DB 仍有欄與 trigger。步驟 3 部署前，含 2a 的 release 必須已部署並經過一輪 bfx-deploy，主機 `ledger_digest.py` 才是不讀本欄的版本。

  - [ ] **2b. ledger／capital scope 拿掉 env**（被擋於：2a 已併入 `dev`）
    - 範圍（修改）：`modules/ledger/__init__.py` 的 `Scope`、`modules/trading/capital.py` 的 `CapitalScope`、`modules/execution/uncertainty_requests.py` 的 `ResolutionScope` 拿掉 `deployment_environment` 欄位與建構參數；`modules/ledger/wiring.py`、`apps/bot.py`、`apps/runtime/daemon.py` 的組裝；`ScopeLock` 實作與 lock 呼叫處改為在呼叫處讀 settings 取 env 算 `derive_transaction_lock_key`；`apps/restore_boot_check.py`、`deploy/vm/pgbackrest/ledger_digest.py` 不再讀 `scope.deployment_environment`。新增 `tests/core/test_realm_value_golden.py`（HKDF info 與 lock key 對固定輸入的 golden 值，取自 `89b06dc4`）。
    - 消費端：`rg -n "Scope\(|CapitalScope\(|ResolutionScope\(" backend/src backend/scripts backend/tests deploy`；`rg -n "\.deployment_environment" backend/src backend/scripts deploy`；`rg -n "derive_transaction_lock_key|xact_lock|writer_lock" backend/src`。
    - 驗收：單元全過；integration（CI）全過；`mypy`（型別上確認沒有呼叫端再傳 env）；golden 測試；(3) 的跨 release 解析仍過。mutation：lock key 呼叫處改成固定字串 `"x"` 而非 settings，golden 要失敗；在 `Scope` 加回 env 欄位，2a 的允許清單檢查（擴充為也檢查這三個型別的欄位）要失敗。

  - [ ] **2c. execution／operator／audit context 拿掉 env**（被擋於：2b 已併入 `dev`）
    - 範圍（修改）：`modules/execution/operator_commands.py` 的 `OperatorContext`、`modules/execution/protocols.py` 的 `AccountContext`、`modules/execution/audit/model.py` 的 `AuditContext` 拿掉 env 欄位與建構參數；`modules/execution/command_gate.py`、`trading_control.py`、`trading_status.py`、`uncertainty_requests.py`、`modules/accounts/*`、`modules/api/*` 的建構與使用；`SealContext` 的 env 改在建構處讀 settings（`core/sealed_credential.py` 本身不改，HKDF info 格式不變）；`modules/api/funding_status.py`、`modules/observability/metrics.py`、`resource.py` 改讀 settings；`REQUEST_COLUMNS` INSERT 的 env 值改在 insert 處讀 settings。
    - 消費端：`rg -n "OperatorContext\(|AccountContext\(|AuditContext\(|SealContext\(" backend/src backend/scripts backend/tests`；`rg -n "\.deployment_environment" backend/src`。
    - 驗收：單元全過；integration（CI）全過；`mypy`；golden 測試仍過（含用既有密文解封的測試：`rg -ln "SealContext" backend/tests` 列出的測試全過）；openapi 無 diff。mutation：`SealContext` 建構處把 env 改成空字串，golden 與解封測試要失敗；API 的 env 欄改成固定 `"x"`，funding status 測試要失敗；在 `OperatorContext` 加回 env 欄位，允許清單檢查要失敗。

- [ ] **3. 縮窄 key、拆 trigger、刪欄（破壞性 migration）**（被擋於：2c 已併入 `dev`；**部署**另被擋於含 2a 的 release 已部署且主機 ops release 已更新——`readlink /usr/local/lib/bfx-ops/current` 指向 2a 以後的 revision，這是只有 owner 能確認的正式環境查詢）
  - 範圍：
    - `backend/alembic/versions/<rev>_drop_realm_columns.py`（新增），照 `inventory.tsv` 的去向：先 `DROP` 6 個 composite FK；4 個 FK 目標換成窄版（UNIQUE 換成步驟 1 的窄 index；`ix_trading_state_scope_id` 同理）；9 個 PK `DROP CONSTRAINT … ADD CONSTRAINT <原名> PRIMARY KEY USING INDEX <窄 index>`；其餘 UNIQUE／部分 unique／索引刪舊的、窄版 `RENAME` 回原名；`ck_trading_state_evidence` 同名重建去掉本欄條件；`ck_sim_venue_event_realm` 刪除（sim venue 只用 ci 庫，由 `modules/simulated_venue/_internal/store_sql.py` 的 `_check_database_realm` 守住，已存在）；6 個 FK 以原名、窄欄位重建（`NOT VALID` 後 `VALIDATE`）；`DROP TRIGGER database_realm_write` 於 31 張 live 表與 12 張 `legacy_archive` 表（只拆 trigger，不碰列；`archive_frozen` 不動）；`DROP FUNCTION guard_database_realm()`；`inventory.tsv` 裡 `trigger-function` 列的 9 個 live function（`guard_ledger_scope`、`guard_ledger_mirror`、`guard_ledger_observation_accept`、`guard_ledger_quarantine_source`、`guard_runtime_policy_head`、`guard_runtime_policy_revision`、`guard_trading_state_transition`、`guard_credential_rotation_request`、`projection_audit.guard_run`）在 `DROP COLUMN` 之前各以 `CREATE OR REPLACE` 改成本體不讀本欄（plpgsql 執行時才解析欄位，不改寫的話刪欄後第一次寫入才失敗；跨表比對 scope 的改成只比 `exchange_account_id`）；`DROP COLUMN deployment_environment` 於 31 張 live 表（column 層級的 grant 隨欄消失）。末尾斷言：非封存 schema 的 `information_schema.columns` 找不到本欄、`pg_trigger` 找不到 `database_realm_write`、`to_regprocedure('public.guard_database_realm()')` 為 NULL、非封存 schema 的 `pg_proc.prosrc` 沒有任何 function 提到本欄、封存表 manifest 雜湊重算不變。
    - ORM 模型拿掉本欄（修改，同步驟 1 的模型檔清單）。
    - grant 清單（修改，與 `DROP COLUMN` 同一個 PR）：四個 `REQUEST_COLUMNS` 拿掉 `deployment_environment`，2a 允許清單檢查移除這個例外。`rg -ln "COLUMNS.*=|GRANT" backend/src | xargs rg -l deployment_environment` 的完整 10 檔（2026-11-08，`89b06dc4`）：
      - grant 常數（步驟 3 改）：`modules/execution/uncertainty_tables.py`（`REQUEST_COLUMNS`）、`modules/execution/capital_tables.py`（`REQUEST_COLUMNS`）、`modules/execution/safety/tables.py`（`REQUEST_COLUMNS`）、`modules/accounts/rotation_tables.py`（`REQUEST_COLUMNS`）。
      - 讀取投影（步驟 2a）：`modules/ledger/read/attempts.py`（`_ATTEMPT_COLUMNS`，含本欄）、`modules/ledger/read/operator_reads.py`（`_RESOLUTION_COLUMNS`）、`modules/ledger/read/match_evidence.py`（`_OFFER_COLUMNS`）——後兩者常數不含本欄，命中來自檔內其他查詢。
      - 常數不含本欄：`modules/execution/operator_commands.py`（`CREDENTIAL_REQUEST_COLUMNS`、`ACTIVE_CREDENTIAL_COLUMNS`；命中來自 `OperatorContext`，2c 處理）。
      - 只有模型：`modules/live_validation/tables.py`（GRANT 只出現在說明文字，是部署 runbook 步驟）、`modules/ledger/tables.py`（`_SCOPE_COLUMNS`，2a 處理；模型欄位步驟 3 拿掉）。
    - grant 與請求欄位測試（修改）：`tests/integration/test_operator_privilege_allowlist.py`、`tests/integration/test_capital_policy_request_roles.py`、`tests/integration/test_credential_rotation_roles.py`、`tests/integration/test_webapi_privilege_allowlist.py`、`tests/integration/test_web_api_high_risk_writes.py`、`tests/integration/test_uncertainty_resolution_outbox.py`；`tests/modules/execution/test_operator_request_schema.py` 改為拿模型的 `REQUEST_COLUMNS` 比對**最新**一支 migration 的欄位常數（步驟 3 的 migration 自帶新的欄位常數，不再比對舊 migration）。
    - `tests/integration/test_drop_realm_columns.py`（新增）。
  - 消費端：`rg -n "deployment_environment" backend/src backend/scripts deploy`（應只剩允許清單）；`rg -n "REQUEST_COLUMNS" backend/src backend/tests backend/alembic`；`rg -n "guard_database_realm|database_realm_write|DISABLE_REALM_TRIGGERS" backend`。
  - 不能動：封存資料列與 manifest；`database_realm` 表與它的不可改 trigger；FK／PK／index 名稱（重建後同名）；開機報告的 `deployment_environment` 鍵（步驟 2 共用的不能動，延後項才拿掉）。
  - 驗收：單元全過；integration（CI）全過；`alembic check` 無 drift；新增測試：(1) head 上 live 表無本欄、無 realm trigger、函式不存在；(2) 窄 FK 仍擋下指向不存在 request 的列；(3) 封存 manifest 重算等於 migration 前；(4) 窄 PK 擋重複；(5) 9 個改寫過的 function 各有一條寫入路徑的測試仍擋下原本擋的情況（跨 account 的 scope 不一致、非法狀態轉移），而且合法寫入通過；四個 grant 測試在新欄位集合下通過。mutation：migration 少改寫一個 function，(1) 的 `prosrc` 斷言要失敗；從 migration 拿掉一張表的 `DROP COLUMN`，(1) 要失敗；把一個 FK 改成不重建，(2) 要失敗；在 migration 複本裡對 `legacy_archive` 表做會改列的動作，(3) 要失敗；把一個 `REQUEST_COLUMNS` 改回含本欄，對應 grant 測試要失敗。`check_inventory.py` 0 errors；2a 的跨 release 解析測試 (3) 仍通過。
  - 停止條件：rehearsal 超過 900 s 預算；manifest 雜湊不等；任何 `VALIDATE` 失敗——停下回報。
  - 需要人做的事：owner 核准含本步的 `dev`→`main` 發版，並確認部署前提（主機 ops release 已是 2a 以後的版本）；發版後驗收。
  - 完成後的狀態：備份標籤早於本 migration 的還原 drill，因為 `t.*` 欄位數不同會回報 `ledger_digest_mismatch`，與 `a6c7e8f9b0d1` 的先例相同，屬設計內；replay 越過本 migration 或之後的備份會相符。步驟 4 把這條寫進 `docs/runbooks/offsite-dr.md`。回滾＝往前修。

- [ ] **4. 測試與文件收乾淨**（被擋於：步驟 3 已併入 `dev`）
  - 範圍：`backend/tests/` 下 118 個測試檔裡的本欄（修改；只為多 realm 植入而存在的測試刪除，列在 `Removed-Test:` trailer、`backend/quality-ratchet.json` 調低）；`tests/pg_templates.py` 的 `DISABLE_REALM_TRIGGERS_SQL`／`disable_realm_triggers`（刪除）；`backend/ARCHITECTURE.md`（修改：schema、ledger scope、invariants）；`docs/runbooks/offsite-dr.md`（修改：步驟 3 的 drill 相容說明）、`docs/runbooks/fresh-host-setup.md`（修改：第 96 行的 trigger 描述拿掉，stamp 仍是一次性步驟）；`docs/adr/2026-11-08-drop-realm-columns.md`（新增），`docs/adr/2026-05-23-deployment-environment-realm-and-backend-ci-gate.md`（修改：標註被取代的部分）。
  - 消費端：`rg -l deployment_environment backend/tests`；`rg -n "deployment_environment|database_realm_write|guard_database_realm" backend/ARCHITECTURE.md docs`。
  - 不能動：仍有意義的 realm 測試（stamp 不可改、開機比對、sim venue 拒非 ci 庫）；跨 release 解析測試。
  - 驗收：單元全過、integration（CI）全過、`quality_ratchet.py` 通過且 trailer 齊；`rg -n deployment_environment backend/tests` 只剩 settings、API、HKDF／lock、開機報告與封存表相關。純文件部分核對指令與路徑。
  - 停止條件：某個要刪的測試其實守著與 realm 無關的行為——改寫不刪，回報。

- [ ] **5. 收尾**：完成定義逐項核對（每步的 CI run 連結與發版後驗收結果填進進度表）；獨立設計審查（design-review，對整串 diff）；`check_inventory.py` 是一次性檢查，留在附件不接 CI，長期守住的是步驟 2a 的允許清單檢查、跨 release 解析測試與步驟 3 的 head 斷言測試（都已在 CI）；延後項（開機報告停止輸出 env）附觸發條件搬回主清單；需要人做的事列給 owner：步驟 1、2a、2b、2c、3 的發版核准；步驟 3 的部署前提確認；各次發版後驗收；延後項觸發時由 owner 跑 `readlink /usr/local/lib/bfx-ops/current` 確認主機 ops 版本。驗收：`python3 docs/plans/2026-11-08-drop-realm-columns/check_inventory.py` 0 errors；`rg -n "guard_database_realm" backend/src deploy` 無結果；允許清單檢查通過；進度表每列有 commit、CI 結果與發版後驗收結果。

### 發版後驗收（每個進 `main` 的步驟：1、2a、2b、2c、3）

migration rehearsal 與還原 drill 只在 owner 核准的 `dev`→`main` 發版、bfx-deploy 部署時於正式主機跑，CI 跑不到。
- 誰觸發：owner 核准並合併發版 PR；bfx-deploy timer 部署時自己跑 restore test（`deploy/vm/systemd/bfx-restore-test@.service`、`bfx-restore-test.timer`）。
- 在哪裡看：`journalctl -u 'bfx-restore-test@*'` 與 `journalctl -u bfx-deploy`；receipt 在 `$HOME/bfx/dr-evidence/restore.json`，旁邊的 stage diagnostic 的 `restore_run_id` 要等於 receipt（`docs/runbooks/offsite-dr.md` 第 369–428 行）。
- 通過條件：receipt 成功、沒有 `restore_output_invalid`／`boot_schema_head_mismatch`／`ledger_bound_invalid`；步驟 1、3 另要 migration rehearsal 在 900 s 內完成；步驟 3 之後若 drill 目標早於該 migration 而回報 `ledger_digest_mismatch`，屬上述設計內情況，需記下 target 標籤。
- 進度表中，這一項在發版前一律記為「未跑－待發版」。

## 決定

- **D0 做不做、怎麼切、值要不要變**（狀態：已決 2026-11-08，Will；擋住步驟：無）
  - 結論：做；分開的 PR；HKDF、lock key、API 值保持不變。ADR：步驟 4 寫 `docs/adr/2026-11-08-drop-realm-columns.md`。
- **D1 每步要不要各自能單獨發版**（狀態：已決 2026-11-08，Will；擋住步驟：無）
  - A：步驟 1 先加窄 index 並讓 guard 替省略的欄補 stamp，每步各自可發版——多一支小 migration，換來每個 PR 都能單獨上線、`dev` 隨時可發版。
  - B：程式與破壞性 migration 同一個 release（未採用）。
  - 結論：A。
- **D2 env 值留在哪裡**（狀態：已決 2026-11-08，Will；擋住步驟：無）
  - A：`Scope` 與各 context 保留 env 欄位，值由 settings 注入（未採用）。
  - B：從 `Scope`、`CapitalScope`、`ResolutionScope`、`OperatorContext`、`AccountContext`、`AuditContext` 與建構參數完全拿掉；需要值的地方（`SealContext` HKDF info、writer／xact lock key、API 欄位、`restore_boot_check` 輸出、metrics／resource 標籤）在該處直接讀 settings。
  - 結論：B；步驟 2 因此拆成 2a、2b、2c。

- **R1 盤點補列：本體讀本欄的其他 trigger function**（2026-11-11，步驟 1 執行時由 head 資料庫的 `pg_proc.prosrc` 查出；不是取捨，沒有替代方案：不改寫就不能刪欄）
  - 原盤點只列名為 `database_realm_write` 的 trigger；另有 9 個 live trigger function 讀本欄。`inventory_probe.py` 加 `trigger-function` 種類，步驟 3 的範圍、末尾斷言與驗收 (5) 跟著補上。

- **R2 兩個 realm 同庫的測試 fixture 提前到步驟 1 處理**（2026-11-11，步驟 1 執行時量到：模型宣告窄 unique index 後單元測試 46 failed／4 errors、非 e2e 的 integration 15 failed，全部是同一個庫替同一個 key 種兩個 realm）
  - 原計畫把測試清理全放在步驟 4；但 D1＝A 讓窄 unique 從步驟 1 就存在，撞到的 fixture 必須同一步改。步驟 4 仍處理其餘測試檔裡的本欄。代價：這幾個 reader 在步驟 1 到 2a 之間仍以 realm 過濾，但不再有「同 key 另一個 realm」的測試證明它；2a 拿掉這個過濾。

拍板順序：D0–D2 都已決，沒有待拍板的題目。

## 進度

狀態值：未開始／進行中／程式完成待驗收／完成。發版後驗收在發版前記「未跑－待發版」。

| 步驟 | 狀態 | 已跑的驗收 | 未跑的驗收與原因 | commit |
|---|---|---|---|---|
| 1 加窄 key、guard 補值 | 完成（發版後驗收待發版） | 本機 2026-11-12：單元 `5360 passed`；非 e2e、非 docker 的 integration `1243 passed, 8 skipped`；新增測試 9 過；mutation 5 個（guard 不補值、拿掉 realm 斷言、`NARROW_INDEXES` 少一列、斷言改掃所有含本欄的表、trigger 順序比較反向）都被對應測試抓到；`alembic check` 無 drift；mypy、ruff、`quality_ratchet.py`、`check_inventory.py`（189 列 0 errors）通過；`/code-review` medium：findings 8（blocking 0），修正 7、記錄 1（R2 的測試缺口，待 owner 接受）；CI 全套通過（PR #371，run 43062394775；integration job 第一次在安裝 PostgreSQL 時下載金鑰 TLS 中斷，重跑通過；squawk 通過） | 發版後驗收：未跑－待發版 | `f54226c8`、`cd821b45`，合併 `ce81165d` |
| 2a SQL 與 digest 不再用本欄 | 未開始 | | 發版後驗收：未跑－待發版 | |
| 2b ledger／capital scope 拿掉 env | 未開始 | | 發版後驗收：未跑－待發版 | |
| 2c execution／operator／audit context 拿掉 env | 未開始 | | 發版後驗收：未跑－待發版 | |
| 3 縮窄 key、拆 trigger、刪欄 | 未開始 | | 發版後驗收：未跑－待發版 | |
| 4 測試與文件 | 未開始 | | | |
| 5 收尾 | 未開始 | | | |
