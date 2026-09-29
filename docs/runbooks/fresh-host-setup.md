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
webauth 在 public 什麼都沒有。

### 1b. 跑 migration

照 [deploy runbook](deploy.md) 第一次部署；bfx-deploy 以 `migrate.env`（owner 的
`DATABASE_URL`）跑 `alembic upgrade head`。migration `e61f3d1ed7ca` 建立 `auth` schema 與
Better Auth 的表。

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

以 `<owner>` 留存下列查詢結果；`bfx_cutover_attest` 與 `bfx_cutover_reader` 的
`rolsuper`、`rolcreaterole`、`rolcreatedb`、`rolbypassrls` 都須為 false；群組
`rolcanlogin=false`，LOGIN `rolinherit=false`：

```sql
SELECT rolname, rolcanlogin, rolinherit, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls
  FROM pg_roles WHERE rolname IN ('bfx_cutover_attest', 'bfx_cutover_reader');
SELECT pg_has_role('bfx_cutover_attest', 'bfx_cutover_reader', 'MEMBER') AS member;
SELECT table_name, privilege_type FROM information_schema.role_table_grants
  WHERE grantee = 'bfx_cutover_reader' AND table_schema = 'public' ORDER BY table_name, privilege_type;
SELECT table_name, column_name, privilege_type FROM information_schema.role_column_grants
  WHERE grantee = 'bfx_cutover_reader' AND table_schema = 'public'
  ORDER BY table_name, column_name, privilege_type;
SELECT n.nspname, p.proname, p.prosecdef,
       has_function_privilege('bfx_cutover_attest', p.oid, 'EXECUTE') AS login_can_execute
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname = 'public' AND p.prosecdef ORDER BY p.proname;
```

群組的 table grants 應為空；column grants 應與 migration 的 `_READER_COLUMNS` 清單完全一致。
其中 `ledger_observation_query` 可讀 `query_id`、`exchange_account_id`、
`deployment_environment`、`query_revision`、
`started_at_ms`、`start_revision`；`ledger_observation` 可讀 `query_id`、
`accept_revision`、`offer_history_pages`、`credit_history_pages`，不再有
`query_started_at_ms` 或 `start_revision`；`accepted_capital_basis` 可讀
`observation_id`、`accept_revision`，不再有 `query_id` 或 `start_revision`。
檢查 `SECURITY DEFINER` 函式清單，確認 LOGIN 沒有可藉以寫入 ledger 的 EXECUTE 權限。
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

### 1d. 驗證隔離（留輸出當 evidence）

```sql
SET ROLE bfx_webauth;
SELECT count(*) FROM auth."user";                 -- OK
SELECT 1 FROM public.position_state LIMIT 1;      -- permission denied
RESET ROLE;

SET ROLE bfx_webapi;
SELECT has_table_privilege(current_user, 'public.position_state', 'SELECT') AS can_read_position,
       has_table_privilege(current_user, 'public.event_log', 'INSERT') AS can_write_event;  -- true, false
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
- `/home/ubuntu/bfx/restore-test.json` 由 DR operator（`ubuntu`）擁有，欄位固定為：

```json
{"account_id": "<canonical UUID>", "environment": "prod", "projector_version": "execution-state-v1"}
```

Restore test 使用本機 `bfx-bot:local` verifier。先核對它對應的 image 與模組；
缺少 image 或下列檢查失敗時，先處理 verifier 來源，不可隨意重 tag：

```bash
sudo docker run --rm --pull=never --entrypoint "" bfx-bot:local /app/.venv/bin/python -c \
  "from bfx_funding_bot.modules.execution.event_store.canonical import rolling_prefix_hash; import scripts.verify_projection_replay; print('ok')"
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
