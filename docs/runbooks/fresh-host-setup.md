# Fresh host setup：DB roles、公開入口與主機工具

新主機（或從零重建的 Postgres）才需要這份。既有 production 已完成，不要重跑。
部署流程本身見 [deploy runbook](deploy.md)；備份與 restore 見 [offsite DR](offsite-dr.md)。

Placeholder：`<owner>` 是 Postgres owner role（`POSTGRES_USER`，migration 用它跑）；
`<public-host>` 是 frontend 對外的 host name。實際值只寫在本機的 `AGENTS.local.md`
或主機上的 secret 檔，不進 git。

## 1. Database roles 與 grants

三個 runtime login role，各自只拿需要的權限：

| role | 誰用 | 權限來源 |
|---|---|---|
| `bfx_bot` | bot daemon、每週報告、研究用一次性容器 | 本節的 default privileges ＋ 各 migration 的收斂 grant |
| `bfx_webapi` | FastAPI web-API | migration `c74d45a54e46` 的 baseline（與之後各 migration）；不碰交易寫入 |
| `bfx_webauth` | Next.js／Better Auth | 只有 `auth` schema（本節手動 grant） |

Migration 對 role 的 grant 都包在 `IF EXISTS (SELECT FROM pg_roles ...)` 裡：role 不存在時
migration 照樣成功但**不會補 grant**。所以順序是：先建 role，再第一次跑 migration。

### 1a. 建 role（migration 之前，以 `<owner>` 執行）

密碼當場產生，直接寫進主機上的 runtime secret 檔（`/opt/bfx/runtime/*.env`，root 0600），
不要貼到終端機歷史、chat 或任何檔案以外的地方。

```sql
CREATE ROLE bfx_bot LOGIN PASSWORD '<read from the secret file>';
CREATE ROLE bfx_webapi LOGIN PASSWORD '<read from the secret file>';
CREATE ROLE bfx_webauth LOGIN PASSWORD '<read from the secret file>';

-- bfx_bot：owner 之後建立的 public 表預設可 DML；受治理的表由各 migration 收斂
-- （例如 capital_*、trading_*、deployments 只給 SELECT/INSERT 或欄位級 UPDATE）。
-- backend/tests/integration/test_capital_migration.py 以同樣的 default privileges 驗證收斂結果。
GRANT USAGE ON SCHEMA public TO bfx_bot;
ALTER DEFAULT PRIVILEGES FOR ROLE <owner> IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bfx_bot;
ALTER DEFAULT PRIVILEGES FOR ROLE <owner> IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO bfx_bot;
```

`bfx_webapi` 與 `bfx_webauth` 在 public schema 不給任何 default privileges：webapi 的讀取
清單與 outbox 欄位級 INSERT 全部由 migration 授予（`test_the_web_api_baseline_is_granted_by_migration_not_by_hand`），
webauth 在 public 什麼都沒有。`bfx_webapi` 在 public 的權限是 migration `d0e1f2a3b4c6` 擁有的精確 allowlist（先 revoke 全部再 grant 回清單，含 `api_keys`、`user_configs` 與 `user_profiles` 的 UPDATE，不再需要手動 grant）；之後新增 webapi 權限的 migration 必須同步更新 `test_webapi_privilege_allowlist.py`。

### 1b. 跑 migration

照 [deploy runbook](deploy.md) 第一次部署；bfx-deploy 以 `migrate.env`（owner 的
`DATABASE_URL`）跑 `alembic upgrade head`。migration `e61f3d1ed7ca` 建立 `auth` schema 與
Better Auth 的表。

### 1b-1. 蓋 database realm（migration 之後，以 `<owner>` 執行一次）

每個資料庫只屬於一個 realm。`database_realm` 是單列、只能插入的表（migration `a7c3e9f1b2d4`），
39 張帶 `deployment_environment` 的表有 trigger，寫入的 realm 與這一列不同、或這一列不存在時一律拒絕
（owner 也一樣）；bot 開機時也會拒絕 `BFX_DEPLOYMENT_ENV` 與這一列不同的資料庫，任何 phase 都適用。

- 已有資料的資料庫（例如 prod）：migration 從現有資料推導並蓋章（只有一種 realm 時），不用手動做；
  有多種 realm 時 migration 失敗，schema 維持原狀。
- 空的資料庫（新主機、模擬用 `bfx_sim`）：migration 後是未蓋章，所有 realm 寫入都被拒絕，owner 蓋章一次：

```sql
-- <realm> 是 prod（新主機）或 shadow（模擬用資料庫，模擬 venue 只接受 shadow／ci）
INSERT INTO database_realm (realm, stamped_at_ms, actor)
VALUES ('<realm>', (extract(epoch FROM clock_timestamp()) * 1000)::bigint, 'owner bootstrap');
```

蓋章後不能 UPDATE／DELETE／TRUNCATE，執行時的 role 沒有寫入權限（`bfx_bot` 只有 SELECT）。
用 `CREATE DATABASE ... TEMPLATE` 複製出來的資料庫會帶著原來的章，要換 realm 就在新庫重建而不是改章。

### 1c. `bfx_webauth` 的 auth schema grant（migration 之後，以 `<owner>` 執行）

`auth` schema 要等 migration 建好才存在，所以這步在 1b 之後：

```sql
GRANT USAGE ON SCHEMA auth TO bfx_webauth;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA auth TO bfx_webauth;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA auth TO bfx_webauth;
ALTER DEFAULT PRIVILEGES FOR ROLE <owner> IN SCHEMA auth
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bfx_webauth;
ALTER DEFAULT PRIVILEGES FOR ROLE <owner> IN SCHEMA auth
  GRANT USAGE, SELECT ON SEQUENCES TO bfx_webauth;
```

`public.user_profiles` 對 `auth.user` 的 FK 檢查由 Postgres 內部執行，webapi 不需要
`auth` 的權限。

### 1c-1. Ledger cutover reader（S1-1 migration 部署後，operator 執行）

Migration 建立 `bfx_cutover_reader NOLOGIN`，只授權新 ledger 表的指定欄位。待部署完成並獲得
operator 授權，另建專用 LOGIN（名稱範例 `bfx_cutover_attest`）並加入群組；密碼用 psql 的
`\password` 互動輸入（不寫進 SQL、shell 歷史或 log），值另存為獨立 secret：

```sql
CREATE ROLE bfx_cutover_attest LOGIN NOINHERIT;
GRANT bfx_cutover_reader TO bfx_cutover_attest;
\password bfx_cutover_attest
```

以 `<owner>` 留存下列查詢結果。`capital_comparison` 在每次執行開頭用 `SET LOCAL ROLE
bfx_cutover_reader` 並自行檢查同一份清單（`apps/capital_comparison_guard.py`），任一項不符就以
機器可讀的 reason 結束（exit 3）；operator 的 attestation 與工具是同一組檢查：

1. `session_user` 是 manifest 的 LOGIN，`SET ROLE bfx_cutover_reader` 後 `current_user` 是群組
   （`reader_role_unavailable`）。
2. 從 LOGIN 沿 `pg_auth_members` 遞迴可達的 role 恰為 {LOGIN, 群組}（`role_closure_unexpected`）。
3. 兩者的 `rolsuper`、`rolcreaterole`、`rolcreatedb`、`rolbypassrls`、`rolreplication` 都是 false
   （`role_attribute_privileged`）；群組 `rolcanlogin=false`（`reader_can_login`），LOGIN
   `rolinherit=false`，且它對群組的那筆 grant `inherit_option=false`（`login_inherits`）；manifest 的 LOGIN 不得是群組本身（`login_is_reader`）。
4. 可達 role 對每個非系統 schema（排除 `pg_catalog`、`information_schema`、`pg_toast*`、`pg_temp*`；
   含 `auth`，掃 `pg_class`，不用固定清單）的每個 relation 沒有 INSERT／UPDATE／DELETE／TRUNCATE／
   TRIGGER／MAINTAIN（`table_write_privilege`），沒有任何欄位 INSERT／UPDATE
   （`column_write_privilege`），對 sequence 沒有 USAGE／UPDATE（`sequence_privilege`）。
   對資料庫與每個非系統 schema 沒有 CREATE（`database_create_privilege`／`schema_create_privilege`）；
   TEMP 不檢查（restore drill 會授予 TEMPORARY，暫存物件影響不到其他 session）。
5. 可達 role 不擁有任何 relation、function、schema（`owned_relation`／`owned_function`／`owned_schema`）。
6. 可達 role 不能 EXECUTE 任何非 trigger 的 `SECURITY DEFINER` function
   （`security_definer_executable`）。
7. 未安裝 `dblink`、`postgres_fdw`（`forbidden_extension`）。

```sql
SELECT rolname, rolcanlogin, rolinherit, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication
  FROM pg_roles WHERE rolname IN ('bfx_cutover_attest', 'bfx_cutover_reader');
SELECT m.inherit_option FROM pg_auth_members m  -- 必須為 false
  JOIN pg_roles l ON l.oid = m.member JOIN pg_roles g ON g.oid = m.roleid
 WHERE l.rolname = 'bfx_cutover_attest' AND g.rolname = 'bfx_cutover_reader';
WITH RECURSIVE closure(oid) AS (
  SELECT oid FROM pg_roles WHERE rolname = 'bfx_cutover_attest'
  UNION SELECT m.roleid FROM pg_auth_members m JOIN closure c ON m.member = c.oid)
SELECT r.rolname FROM closure c JOIN pg_roles r ON r.oid = c.oid ORDER BY 1;  -- 恰兩列
-- 檢查 4～6 對每個可達 role 各跑一次（把 :role 換成兩個 role）：回傳列必須為空
SELECT n.nspname, c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp%'
    AND ((c.relkind IN ('r','p','v','m','f')
          AND (has_table_privilege(:'role', c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,MAINTAIN')
               OR has_any_column_privilege(:'role', c.oid, 'INSERT,UPDATE')))
         OR (c.relkind = 'S' AND has_sequence_privilege(:'role', c.oid, 'USAGE,UPDATE')));
SELECT 'relation', relname::text FROM pg_class WHERE relowner = :'role'::regrole
UNION ALL SELECT 'function', proname::text FROM pg_proc WHERE proowner = :'role'::regrole
UNION ALL SELECT 'schema', nspname::text FROM pg_namespace WHERE nspowner = :'role'::regrole
UNION ALL SELECT 'secdef', p.proname::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE p.prosecdef AND p.prorettype <> 'trigger'::regtype
    AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND has_function_privilege(:'role', p.oid, 'EXECUTE');
SELECT 'database', datname FROM pg_database
  WHERE datname = current_database() AND has_database_privilege(:'role', oid, 'CREATE')
UNION ALL SELECT 'schema', nspname FROM pg_namespace
  WHERE nspname NOT IN ('pg_catalog', 'information_schema')
    AND nspname NOT LIKE 'pg\_toast%' AND nspname NOT LIKE 'pg\_temp%'
    AND has_schema_privilege(:'role', oid, 'CREATE');
SELECT extname FROM pg_extension WHERE extname IN ('dblink', 'postgres_fdw');  -- 空
-- 群組只有欄位授權：table grants 必須為空；column grants 與下列清單完全一致
SELECT table_name, privilege_type FROM information_schema.role_table_grants
  WHERE grantee = 'bfx_cutover_reader' AND table_schema IN ('public', 'legacy_archive')
  ORDER BY table_name, privilege_type;
SELECT table_name, column_name, privilege_type FROM information_schema.role_column_grants
  WHERE grantee = 'bfx_cutover_reader' AND table_schema IN ('public', 'legacy_archive')
  ORDER BY table_name, column_name, privilege_type;
```

群組的 table grants 應為空；column grants 應與 ledger migrations 的 `_READER_COLUMNS` 清單，加上
`b5c6d7e8f9a0`（`apps/capital_comparison` 的讀取，下列）與 `d7e8f9a0b1c2`（cutover 兩個 arm 的讀取，下列）
合併後完全一致，全部只有 SELECT：

- legacy 十張表 `event_log`、`event_prefix_hashes`、`capital_policy_heads`、`capital_policy_revisions`、
  `capital_snapshots`、`capital_snapshot_queries`、`execution_decisions`、`projection_heads`、
  `submission_attempts`、`execution_uncertainties`：baseline 讀整列，所以是 ORM 全部欄位（其中
  凍結的表自 `c2d3e4f5a6b7` 在 `legacy_archive`，欄位授權隨表搬過去）。
- inventory 掃描欄位：`offer_claims`（scope、`state`、`symbol`）、`trading_state`（scope）、
  `uncertainty_resolution_requests`／`capital_policy_requests`／`trading_control_requests`（scope、`state`）。
- ledger 讀取補充：`accepted_capital_basis_symbol` 的 `conservation`、`lent_unexplained`、
  `foreign_executed`、`fill_conflicts`；`submission_attempt_journal.intended_amount`
  （`authorization_evidence` 仍不可讀）。
- `d7e8f9a0b1c2`：`offer_claims`、`funding_trades` 全部欄位（legacy arm 的 `_classify` 讀整列）；
  `submission_attempt_journal` 的 `normalized_payload`、`seed_provenance`；`ledger_observation.origin`；
  `trading_state.id`；`uncertainty_resolution_requests` 的 `request_id`、`outcome_reason`；
  `capital_policy_requests`／`trading_control_requests` 的 `request_id`；`venue_offer_state`／
  `venue_credit_state` 的 scope、`symbol`、`is_terminal`。

其餘 ledger 欄位維持原 allowlist：
其中 `ledger_observation_query` 可讀 `query_id`、`exchange_account_id`、
`deployment_environment`、`query_revision`、
`started_at_ms`、`start_revision`；`ledger_observation` 可讀 `query_id`、
`accept_revision`、`offer_history_pages`、`credit_history_pages`；
`accepted_capital_basis` 可讀 `observation_id`、`accept_revision`、`attempt_seq_high_water`、`scope_block`（不再有 `policy_revision_id`、`authorization_block`、`credit_cells_present`）；`accepted_capital_basis_symbol` 可讀 `block`；`submission_attempt_journal` 可讀 `cell_id`；`ledger_observation` 可讀 `trades_complete` 與 trade 請求範圍；`ledger_observation_wallet` 可讀 `symbol`；
`quarantine_member` 可讀 `source_kind`、`venue_object_id`；`quarantine_opening` 可讀 `opened_revision`、`source_attempt_id`，
`accepted_capital_basis_credit`、`accepted_capital_basis_credit_cell`、`ledger_observation_trade` 與 `capital_authority_epoch` 的所有欄位可讀。
接著以 LOGIN 連線，在 **read-write transaction** 執行以下拒絕檢查；每個預期失敗的
statement 都各自開新 transaction，避免前一個錯誤使後續 statement 自動失敗：

```sql
BEGIN READ WRITE;
SET LOCAL ROLE bfx_cutover_reader;
SELECT id, accepted FROM public.ledger_observation LIMIT 1; -- succeeds
SELECT query_id, query_revision, started_at_ms, start_revision
  FROM public.ledger_observation_query LIMIT 1; -- succeeds
ROLLBACK;

BEGIN READ WRITE;
SET LOCAL ROLE bfx_cutover_reader;
SELECT evidence FROM public.ledger_observation LIMIT 1; -- permission denied
ROLLBACK;

BEGIN READ WRITE;
SET LOCAL ROLE bfx_cutover_reader;
DELETE FROM public.ledger_observation WHERE false; -- permission denied
ROLLBACK;

BEGIN READ WRITE;
SET LOCAL ROLE bfx_cutover_reader;
SELECT nextval('public.trading_state_id_seq'); -- permission denied (or sequence absent)
ROLLBACK;
```

再用 LOGIN 本身（未 `SET ROLE`）確認不能讀 ledger；它只因成員資格能明確 `SET ROLE`
為 reader。此 role 只供 cutover／rehearsal reader 使用，不放進 bot 或 webapi 的連線設定。

### 1c-2. `legacy_archive`（migration `c2d3e4f5a6b7` 建立，不需手動 grant）

切換前 legacy authority 的 12 張凍結表（`event_log`、`event_prefix_hashes`、`projection_heads`、
`position_state`、`venue_offer_state`、`venue_credit_state`、`reconcile_observation`、`offer_claims`、
`submission_attempts`、`execution_uncertainties`、`capital_snapshot_queries`、`capital_snapshots`）
在 `legacy_archive` schema。public 的 default privileges 不作用於它：migration 撤銷所有非 owner
role 對這些表與其 sequence 的權限，只授回 `bfx_webapi` 讀 `event_log` 的 8 個欄位（archived
execution history）與 `bfx_cutover_reader` 原有的欄位讀取（switch scaffolding，PR-D 移除），兩者各
有 schema USAGE。`legacy_archive.manifest` 記錄每表的列數、內容 SHA-256 與被撤銷的權限（downgrade
依此授回），只有 owner 能讀。全新主機的空資料庫照樣經舊 migration 在 public 建表，再由這個
migration 搬過去。

### 1d. 驗證隔離（留輸出當 evidence）

```sql
SET ROLE bfx_webauth;
SELECT count(*) FROM auth."user";                 -- OK
SELECT 1 FROM public.trading_state LIMIT 1;       -- permission denied
RESET ROLE;

SET ROLE bfx_webapi;
SELECT has_table_privilege(current_user, 'public.trading_state', 'SELECT') AS can_read_state,
       has_column_privilege(current_user, 'legacy_archive.event_log', 'payload', 'SELECT')
         AS can_read_archived_history,
       has_table_privilege(current_user, 'legacy_archive.event_log', 'INSERT')
         AS can_write_archive;  -- true, true, false
RESET ROLE;
```

### 1e. 連線字串（寫進 secret 檔，不進 git）

| 檔案（`/opt/bfx/runtime/`） | `DATABASE_URL` 的 role | 備註 |
|---|---|---|
| `bot.env` | `bfx_bot` | 範本 `deploy/vm/bot.env.example` |
| `webapi.env` | `bfx_webapi` | 同檔還有 `BETTER_AUTH_JWKS_URL=http://bfx-frontend:3000/api/auth/jwks`（docker 內網） |
| `frontend.env` | `bfx_webauth` | direct 連線（`@postgres:5432/bfx`，無 pooler、無 sslmode）；範本 `deploy/vm/frontend.env.example` |
| `migrate.env` | `<owner>` | 只有 bfx-deploy 的 migration 與 operator one-shot 使用 |

`BETTER_AUTH_SECRET` 以 `openssl rand -base64 48` 在主機上產生後直接寫進 `frontend.env`
（read from the secret file；不要在任何文件或 chat 裡出現實際值）。

## 2. Frontend public ingress

公開面只有 frontend container。它只綁 host loopback `127.0.0.1:3001`
（`deploy/vm/docker-compose.app.yml`；host 的 3000 是 Grafana）。web-API 不公開：FE
server-side proxy 經 docker 網路打 `http://webapi:8000`，JWKS 也走內網。主機不開任何
雲端 security-list port，TLS 由入口服務終結。

任何能「公開 HTTPS 443 → 主機 `127.0.0.1:3001`」的入口都可以，例如：

- **Tailscale Funnel**（目前使用）：主機裝好 Tailscale，tailnet 開啟 Funnel、MagicDNS 與
  HTTPS certificates 後執行
  `tailscale funnel --bg --https=443 http://127.0.0.1:3001`，`tailscale funnel status` 顯示公開 URL。
- **Cloudflare Tunnel**：`cloudflared` 建 tunnel，把 `<public-host>` 的 ingress 指到
  `http://127.0.0.1:3001`；DNS 由 Cloudflare 管。

入口確定後，`<public-host>` 要同步到這些地方（改 host 就是改 image，需要一次部署）：

| 位置 | 值 |
|---|---|
| GitHub repository variables `NEXT_PUBLIC_APP_URL`、`NEXT_PUBLIC_BETTER_AUTH_URL` | `https://<public-host>`（CI build 時 bake 進 client bundle） |
| GitHub repository variable `NEXT_PUBLIC_APP_NAME` | `bfx-funding-bot` |
| `/opt/bfx/runtime/frontend.env` 的 `BETTER_AUTH_URL`、`NEXT_PUBLIC_*` | 同上 |
| `/opt/bfx/runtime/frontend.env` 的 `PASSKEY_RP_ID` | `<public-host>`（只有 host，不含 scheme）；換 host 會讓既有 passkey 失效 |

驗證：先在主機上 `curl -fsS http://127.0.0.1:3001/` 確認 FE 起來，再從外部開
`https://<public-host>` 走一次登入 → dashboard（`/api/proxy/*` 回 200）。回滾：把入口指回
舊目標或停掉入口服務；bot 與 webapi 不受影響。

## 3. 首次安裝主機工具

本節安裝 `bfx-deploy`、告警與 DR units；不會建立 Postgres／Redis、runtime secrets、
帳戶資料或 pgBackRest repository。先完成資料服務與 `bfx_default` network、上面的
roles／連線設定，以及 [offsite DR](offsite-dr.md) 的備份與 restore 前置作業。
部署前須有可用的 restore test；空資料庫與缺少 verifier image 的主機不能直接套用
既有環境的部署流程。

### 3a. 安裝工具與 units

在 Linux VM 的乾淨 checkout `/home/ubuntu/bfx-funding-bot`，核對 HEAD 是要安裝的
release revision；CI 與 Release images 必須已成功。主機需要 root 擁有的
`/usr/local/bin/uv`、Docker buildx 與 Python 3.12 以上：

```bash
/usr/local/bin/uv --version
docker buildx version
/usr/bin/python3 --version
sudo /home/ubuntu/bfx-funding-bot/deploy/vm/ops/install.sh
ls -l /usr/local/lib/bfx-ops/current /home/ubuntu/bfx-releases/current
```

安裝器從 git 物件寫出 `/usr/local/lib/bfx-ops/releases/<rev>`，依 `ops/uv.lock`
執行 `uv sync --frozen`，建立 `bfx-deploy`／`bfx-notify` wrappers 與
`/home/ubuntu/bfx-releases/<rev>` DR checkout，再安裝 `systemd/managed-units`
列出的 units。它不會啟用或啟動任何 timer。之後工具由成功部署的 release 自動更新。

### 3b. Runtime 設定與首次驗證

- `/opt/bfx/runtime/{bot,webapi,frontend,migrate,notify,ghcr}.env` 依
  [deploy env 規則](deploy.md#env-檔規則) 準備，root 擁有且 mode `0600`。
  bot／webapi 使用已核對的 canonical `BFX_EXCHANGE_ACCOUNT_ID` 與相同的
  `BFX_OPERATOR_USER_ID`；不沿用舊 launcher 的 `BFX_RELEASE_*`。
- 告警所需 `TELEGRAM_BOT_TOKEN`／`TELEGRAM_CHAT_ID` 同時配置到 `notify.env` 與
  `bot.env`；GHCR 憑證只需 `read:packages`。設定方式見 [operations](operations.md#6-告警)。

Restore test 不需要設定檔（ledger mode 驗證 restored copy 內的每個 scope），使用本機
`bfx-bot:local` 跑唯讀 boot check。先核對它對應的 image 與模組；缺少 image 或下列檢查
失敗時，先處理 verifier 來源，不可隨意重 tag：

```bash
sudo docker run --rm --pull=never --entrypoint "" bfx-bot:local /app/.venv/bin/python -c \
  "from bfx_funding_bot.apps.authority_support import require_ledger_seed; from bfx_funding_bot.modules.ledger.wiring import build_ledger_capital_reader; print('ok')"
sudo systemctl start --no-block bfx-restore-test@current.service
journalctl -fu bfx-restore-test@current.service
```

確認該 service 成功，且 `/home/ubuntu/bfx/dr-evidence/restore-heartbeat.json` 是本次
產生的新 evidence。再執行 `sudo bfx-notify --level info "bfx setup: notify test"`，
確認告警送達；最後執行 `sudo bfx-deploy --dry-run`，核對 revision、CI run、
migration／restore 計畫、rollback target 與所有 blockers。

若出現 `foreign_container_holds_name:bfx-*`，表示同名 container 不屬於 `bfx-app`。
先記錄其 image、restart policy 與用途，安排停機並保留回復副本後才釋放名稱；
bfx-deploy 不會替 operator 刪除它們。不要啟用 `legacy-app` profile。

### 3c. 首次部署與排程

Dry run 無 blocker 後，以 systemd 執行，避免 SSH 斷線中斷部署：

```bash
sudo systemctl start --no-block bfx-deploy.service
journalctl -fu bfx-deploy.service
```

依 [deploy runbook](deploy.md) 核對 ledger 的 `started`／`deployed`、三個 app container
的 `bfx-app` ownership 與 digest、health、工具與 DR checkout 的 `current` revision。
新資料庫在 migration 後完成 §1c 的 auth grants 與 §1d 的隔離驗證；再驗證登入、TOTP、
uncertainty／history 頁面與告警。交易狀態與包絡依 [operations](operations.md) 驗證，
部署本身不會 resume，也沒有 build 核准或限額期。

第一次部署尚無成功的 rollback target；失敗時依 deploy runbook 處理，不能假定會自動回滾。
確認手動部署、備份與 restore 檢查成功後，才啟用排程：

```bash
sudo systemctl enable --now bfx-deploy.timer bfx-backup-check.timer bfx-restore-test.timer
systemctl list-timers 'bfx-*'
```

pgBackRest backup／status timers 依 [offsite DR](offsite-dr.md) 分別驗證及啟用；每週報告
排程依 [operations §8](operations.md#8-定期與背景工作)。確認各 job 自己的結論，
不要只以 timer 已啟用判定成功。後續無 migration 的 release 可依
[rollback drill](deploy.md#6-rollback-drill刻意走一次回滾路徑) 驗證回滾路徑。
