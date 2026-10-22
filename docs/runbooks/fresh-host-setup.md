# Fresh host setup：DB roles、公開入口與主機工具

新主機（或從零重建的 Postgres）才需要這份。既有 production 已完成，不要重跑。
部署流程本身見 [deploy runbook](deploy.md)；備份與 restore 見 [offsite DR](offsite-dr.md)。

Placeholder：`<owner>` 是 Postgres owner role（`POSTGRES_USER`，migration 用它跑）；
`<ops-host>` 是營運入口節點的 MagicDNS 名稱（`bfx-ops.<tailnet>.ts.net`）。實際值只寫在本機的 `AGENTS.local.md`
或主機上的 secret 檔，不進 git。

## 0. 資料服務的 env 檔

`docker-compose.bot.yml` 的 postgres 只讀 `.env.postgres.runtime`（與 compose 檔同目錄，0600），
內容只有 `POSTGRES_USER`（＝`<owner>`）、`POSTGRES_PASSWORD`、`POSTGRES_DB` 三行；
`POSTGRES_PASSWORD` 只在空的 PGDATA 第一次 initdb 時用到。不要把 app 的 runtime env
（KEK、連線字串、API key）放進這個檔：資料庫容器用不到它們，`docker inspect` 卻看得到。

## 1. Database roles 與 grants

四個 runtime login role，各自只拿需要的權限：

| role | 誰用 | 權限來源 |
|---|---|---|
| `bfx_bot` | bot daemon | 本節的 default privileges ＋ 各 migration 的收斂 grant |
| `bfx_webapi` | FastAPI web-API | migration `c74d45a54e46` 的 baseline（與之後各 migration）；不碰交易寫入 |
| `bfx_operator` | operator CLI（one-off `bfx-operator` compose service） | migration `4ab02765a34b` 的精確 allowlist：三個 operator outbox 的 request 欄位 INSERT 與驗證所需的讀取；沒有 UPDATE／DELETE |
| `bfx_research` | 每週報告（`bfx-weekly-report`）、研究用一次性容器 | migration `3c5e7a9b1d2f` 的精確 allowlist：只寫研究表（`funding_stats`、`perp_funding_rates`、`liquidations` 的 upsert，`attribution_weekly`、`fill_rate_stats` 的 delete＋insert，`fill_rate_model_artifacts` 的 insert），只讀每週步驟與研究腳本用到的表；不能寫 ledger、`trading_state`、outbox，讀不到憑證表 |

模擬 soak 另有第五個 login role `bfx_sim`，只連自己的 database `bfx_sim`、在那裡以 `bfx_bot` 身分執行；
它的建立與 grants 在 [simulation-soak.md](simulation-soak.md) §2，不屬於新主機的必要步驟。

Migration 對 role 的 grant 都包在 `IF EXISTS (SELECT FROM pg_roles ...)` 裡：role 不存在時
migration 照樣成功但**不會補 grant**。所以順序是：先建 role，再第一次跑 migration。

**已上線的主機加新 role**（例如 `bfx_research`）：merge 進 `main` 就會由 bfx-deploy 自動部署並跑 migration，
所以 role 與它的 secret 檔（`research.env`）要在**合併那個 PR 之前**於 VM 建好（§1a、§1e），部署後照 §1d 核對。
若已部署才建 role，migration 不會重跑：以 `<owner>` 執行該 migration 的 REVOKE／GRANT（`3c5e7a9b1d2f` 的
`RESEARCH_TABLE_PRIVILEGES`，逐表 `GRANT ... ON public.<table> TO bfx_research`，另加 `GRANT USAGE ON SCHEMA public`），
再跑 §1d。

### 1a. 建 role（migration 之前，以 `<owner>` 執行）

密碼當場產生，直接寫進主機上的 runtime secret 檔：`bfx_bot`、`bfx_webapi` 寫秘密檔
`/opt/bfx/runtime/secrets/<service>/DATABASE_URL`（root:4600 0640），其他 role 寫 `/opt/bfx/runtime/*.env`（root 0600）。
不要貼到終端機歷史、chat 或任何檔案以外的地方；含密碼的 `CREATE ROLE` 不在互動式 psql 裡打（psql 會寫 history），
照 §1e 在 root shell 裡經 stdin 送進去。`<owner>` 必須是 superuser（官方 image 的
`POSTGRES_USER` 就是）：下面兩個 `SET` 是 superuser 參數，讓失敗的 `CREATE ROLE` 不會把含明文密碼的
STATEMENT 寫進 PG log。

```sql
SET log_statement = 'none';
SET log_min_error_statement = 'panic';
CREATE ROLE bfx_bot LOGIN PASSWORD '<read from the secret file>';
CREATE ROLE bfx_webapi LOGIN PASSWORD '<read from the secret file>';
CREATE ROLE bfx_operator LOGIN PASSWORD '<read from the secret file>';
CREATE ROLE bfx_research LOGIN PASSWORD '<read from the secret file>';

-- bfx_bot：owner 之後建立的 public 表預設可 DML；受治理的表由各 migration 收斂
-- （例如 capital_*、trading_*、deployments 只給 SELECT/INSERT 或欄位級 UPDATE）。
-- backend/tests/integration/test_capital_migration.py 以同樣的 default privileges 驗證收斂結果。
GRANT USAGE ON SCHEMA public TO bfx_bot;
ALTER DEFAULT PRIVILEGES FOR ROLE <owner> IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bfx_bot;
ALTER DEFAULT PRIVILEGES FOR ROLE <owner> IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO bfx_bot;
```

`bfx` 的 CONNECT 只給上面四個 runtime role（`<owner>` 是 superuser，不受影響）。官方 image 以 `POSTGRES_DB`
建立的 database 預設對 PUBLIC 開放 CONNECT；收回後，同一 cluster 的其他 login role（例如模擬 soak 的 `bfx_sim`）
連不進 `bfx`，role membership 也不會帶進來（membership 用 `INHERIT FALSE`）：

```sql
GRANT CONNECT ON DATABASE bfx TO bfx_bot, bfx_webapi, bfx_operator, bfx_research;
REVOKE CONNECT ON DATABASE bfx FROM PUBLIC;
```

之後新增連 `bfx` 的 runtime role，要在這裡一併 `GRANT CONNECT`，否則它連不上（`permission denied for database "bfx"`）。
還原演練的驗證 role 自己授予 CONNECT（`deploy/vm/pgbackrest/restore_drill.py`），不依賴 PUBLIC。

`bfx_webapi` 在 public schema 不給任何 default privileges：webapi 的讀取
清單與 outbox 欄位級 INSERT 全部由 migration 授予（`test_the_web_api_baseline_is_granted_by_migration_not_by_hand`）。`bfx_operator` 同樣不給 default privileges，它的權限由 migration `4ab02765a34b` 先 revoke 全部（含 schema CREATE）再 grant 回清單，`test_operator_privilege_allowlist.py` 比對 effective privileges。`bfx_research` 也不給 default privileges：migration `3c5e7a9b1d2f` 先 revoke 它在 `public` 與 `legacy_archive` 的全部權限（含 schema CREATE）再 grant 回清單，`test_research_privilege_allowlist.py` 比對 effective privileges，`test_research_role_behaviour.py` 以這個 role 登入跑每週步驟的實際語句。`bfx_webapi` 在 public 的權限是 migration `d0e1f2a3b4c6` 擁有的精確 allowlist（先 revoke 全部再 grant 回清單，含 `api_keys`、`user_configs` 與 `user_profiles` 的 UPDATE，不再需要手動 grant）；之後新增 webapi 權限的 migration 必須同步更新 `test_webapi_privilege_allowlist.py`。

### 1b. 跑 migration

照 [deploy runbook](deploy.md) 第一次部署；bfx-deploy 以 `migrate.env`（owner 的
`DATABASE_URL`）跑 `alembic upgrade head`。

### 1b-1. 蓋 database realm（migration 之後，以 `<owner>` 執行一次）

每個資料庫只屬於一個 realm。`database_realm` 是單列、只能插入的表（migration `a7c3e9f1b2d4`），
每張帶 `deployment_environment` 的表都有 `database_realm_write` trigger（實際清單可查：
`SELECT tgrelid::regclass FROM pg_trigger WHERE tgname = 'database_realm_write' ORDER BY 1;`），
寫入的 realm 與這一列不同、或這一列不存在時一律拒絕（owner 也一樣）；bot 開機時也會拒絕 `BFX_DEPLOYMENT_ENV` 與這一列不同的資料庫，任何 phase 都適用。

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

### 1c. Better Auth 的 `auth` schema 與 `bfx_webauth`（已退役，9d-7）

migration 鏈前段的 `e61f3d1ed7ca` 會建立 `auth` schema，`f836fdee9bf3` 再把它連同
`bfx_webauth` role 一起刪除（role 不存在時略過；同一 cluster 若還有別的資料庫授權給它，role 會留下但在這個資料庫沒有任何權限）。新主機不建立 `bfx_webauth`，也不需要任何 `auth` grant。

### 1c-1. Ledger cutover reader（已退役，S1-8 PR-D）

`bfx_cutover_reader`（NOLOGIN 群組，`b1e2d3a4c5f6` 建立）只供切換用的 capital comparison／closure
verifier／comparison rehearsal 以 `SET LOCAL ROLE` 讀取；2026-10-05 切換完成，S1-8 PR-D 刪除那些工具，
migration `d3e4f5a6b7c8` 撤銷它在資料庫內的全部權限並 drop 這個群組（同一 cluster 若還有別的資料庫授權給它，
群組會留下但在這個資料庫沒有任何權限）。新主機不需要建立 reader，也不需要 LOGIN。

operator 收尾（在 `d3e4f5a6b7c8` 部署之後，一次；需 Will 授權）：

```sql
-- 以 <owner> 執行。群組應已不存在；若還在，先確認沒有其他資料庫授權給它，再 drop。
SELECT rolname FROM pg_roles WHERE rolname IN ('bfx_cutover_reader', 'bfx_cutover_attest');
DROP ROLE bfx_cutover_attest;                   -- 切換時建立的 LOGIN；群組 drop 後已無 membership
-- DROP ROLE IF EXISTS bfx_cutover_reader;      -- 只有上面查詢仍列出群組時
```

再刪除主機上的密碼檔 `/opt/bfx/runtime/cutover-reader.env`（`sudo rm`）。

### 1c-2. `legacy_archive`（migration `c2d3e4f5a6b7` 建立，不需手動 grant）

切換前 legacy authority 的 12 張凍結表（`event_log`、`event_prefix_hashes`、`projection_heads`、
`position_state`、`venue_offer_state`、`venue_credit_state`、`reconcile_observation`、`offer_claims`、
`submission_attempts`、`execution_uncertainties`、`capital_snapshot_queries`、`capital_snapshots`）
在 `legacy_archive` schema。public 的 default privileges 不作用於它：migration 撤銷所有非 owner
role 對這些表與其 sequence 的權限，只授回 `bfx_webapi` 讀 `event_log` 的 8 個欄位（archived
execution history）與 schema USAGE（`bfx_cutover_reader` 原有的欄位讀取已由 `d3e4f5a6b7c8` 撤銷）。`legacy_archive.manifest` 記錄每表的列數、內容 SHA-256 與被撤銷的權限（downgrade
依此授回），只有 owner 能讀。12 張表與 manifest 對任何 role（含 owner）都拒絕寫入（`archive_frozen` trigger）。全新主機的空資料庫照樣經舊 migration 在 public 建表，再由這個
migration 搬過去。

### 1d. 驗證隔離（留輸出當 evidence）

```sql
SET ROLE bfx_webapi;
SELECT has_table_privilege(current_user, 'public.trading_state', 'SELECT') AS can_read_state,
       has_column_privilege(current_user, 'legacy_archive.event_log', 'payload', 'SELECT')
         AS can_read_archived_history,
       has_table_privilege(current_user, 'legacy_archive.event_log', 'INSERT')
         AS can_write_archive;  -- true, true, false
RESET ROLE;

SET ROLE bfx_operator;
SELECT has_column_privilege(current_user, 'public.trading_control_requests', 'reason', 'INSERT')
         AS can_queue,
       has_table_privilege(current_user, 'public.trading_control_requests', 'UPDATE')
         AS can_settle,
       has_table_privilege(current_user, 'public.trading_state', 'INSERT')
         AS can_write_state;  -- true, false, false
RESET ROLE;

SET ROLE bfx_research;
SELECT has_table_privilege(current_user, 'public.funding_stats', 'UPDATE') AS can_upsert_research,
       has_table_privilege(current_user, 'public.trading_state', 'INSERT') AS can_write_state,
       has_table_privilege(current_user, 'public.submission_attempt_journal', 'INSERT')
         AS can_write_ledger,
       has_any_column_privilege(current_user, 'public.exchange_account_credentials', 'SELECT')
         AS can_read_credentials;  -- true, false, false, false
RESET ROLE;

SELECT has_database_privilege('public', 'bfx', 'CONNECT') AS public_connects;  -- false
```

### 1e. 連線字串（寫進 secret 檔，不進 git）

bot 與 webapi 的 `DATABASE_URL` 是秘密檔，格式與規則見 [deploy「秘密檔」](deploy.md#秘密檔botwebapi9h)。先建
group 4600 與目錄（[deploy 9h 切換第 1 步](deploy.md#9h-切換一次性依序在-merge-9h-的-pr-之前做第-13-步)），再寫入。
每個 role 一個 root shell，用 [secret-rotation「通用寫法」](secret-rotation.md#通用寫法) 的範本（值不上 argv、不進 history），
把範本最後的 `V=...` 與 `swap` 換成：

```bash
P=$(openssl rand -hex 32)                     # role 的密碼
V="<scheme>://bfx_bot:$P@<host>:5432/bfx"     # 寫進檔案的 URL（root shell 裡的字串組合，不上 argv）
swap /opt/bfx/runtime/secrets/bot/DATABASE_URL root:4600 0640 printf '%s' "$V"
# env 檔的 role（operator.env、research.env）：檔裡先放一行 `DATABASE_URL=`，上一行改成 setkey <env 檔> DATABASE_URL
printf "SET log_statement='none'; SET log_min_error_statement='panic'; CREATE ROLE bfx_bot LOGIN PASSWORD '%s';\n" "$P" \
  | docker exec -i --user postgres bfx-postgres psql -U <owner> -d bfx -v ON_ERROR_STOP=1 -q
```

其他 role 的連線字串照舊寫在 env 檔，也用上面這段（換 role 名稱與檔案）。

| 檔案（`/opt/bfx/runtime/`） | `DATABASE_URL` 的 role | 備註 |
|---|---|---|
| `secrets/bot/DATABASE_URL` | `bfx_bot` | bot 的其他秘密檔（`BFX_ADMIN_TOKEN`、`BFX_ADMIN_READ_TOKEN`、`BFX_VAULT_KEK`、`TELEGRAM_BOT_TOKEN`）同目錄；非秘密設定在 `bot.env`（範本 `deploy/vm/bot.env.example`） |
| `secrets/webapi/DATABASE_URL` | `bfx_webapi` | 同目錄還有 `BFX_ADMIN_READ_TOKEN`（與 bot 的同值，`openssl rand -hex 32`）。`webapi.env` 放開機必填的入口設定（缺任一個 webapi 拒絕開機）：`BFX_OPERATOR_TAILNET_USER_ID`（operator 的 tailnet user id，`tailscale whois --json <operator 裝置的 tailnet IP>` 的 `UserProfile.ID`；也是 operator membership 的 `user_id`）、`BFX_WEBAPI_ALLOWED_HOSTS=<ops-host>`、`BFX_FE_SENTRY_DSN`（與 repository variable `NEXT_PUBLIC_SENTRY_DSN` 同值）。webapi **不可**有 `BFX_ADMIN_TOKEN` 或 `BFX_VAULT_KEK`（env 的 key 或秘密檔都不行），看到任一個（即使值是空的）就拒絕開機（halt token 與 KEK 只給 bot） |
| `operator.env` | `bfx_operator` | 另有 `BFX_EXCHANGE_ACCOUNT_ID`（照抄 `bot.env`）；誰在操作不寫這裡：`bfx-op` 每次以 `tailscale whois` 取得呼叫者的 tailnet user id 當 `requested_by`，CLI 只接受該帳戶的 member；`BFX_DEPLOYMENT_ENV` 不寫這裡，由 `bfx-op` 從部署中版本的 `live.env` 傳入；compose 檔 `deploy/vm/ops/docker-compose.operator.yml`，只由 `sudo bfx-op` 啟動（[operations §10](operations.md#10-operator-clibfx-op)）。不在 bfx-deploy 的部署前檢查裡：`bfx-op` 每次呼叫時檢查 root 0600 |
| `research.env` | `bfx_research` | 另有 `BFX_EXCHANGE_ACCOUNT_ID`（照抄 `bot.env`），只有這兩個 key。每週報告的 runner（`bfx_weekly_report.py`）每次執行時檢查 root 0600，不存在就拒跑、不會退回 `bot.env`；研究用一次性容器也讀它（[research-one-shot-jobs](research-one-shot-jobs.md)） |
| `migrate.env` | `<owner>` | 只有 bfx-deploy 的 migration 與 operator one-shot 使用 |

## 2. Tailnet 入口（營運 UI）

沒有公開入口：營運 UI 與 webapi 只能從 tailnet 內、經 Tailscale Serve 連到（9d-5）。webapi 只聽
`127.0.0.1:8000`，在 holder `bfx-netns` 的 namespace 裡；節點 `bfx-tailscale`（`tag:bfx-ops`）
與它共用 namespace，Serve 把 `https://<ops-host>/` 轉給它（`deploy/vm/tailscale/serve.json`）。
主機不開任何雲端 security-list port，TLS 由 Serve 終結（tailnet 開啟 MagicDNS 與 HTTPS certificates）。

1. tailnet policy（admin console → Access controls，Will 存檔）：`tagOwners` 有 `"tag:bfx-ops": ["autogroup:admin"]`；
   一條 grant 只讓 operator 連 `tag:bfx-ops` 的 `tcp:443`，並帶 operator capability：
   `{"src": ["<operator login>"], "dst": ["tag:bfx-ops"], "ip": ["tcp:443"],
   "app": {"github.com/Will413028/bfx-funding-bot/cap/operator": [{"user_id": "<BFX_OPERATOR_TAILNET_USER_ID>"}]}}`。
   webapi 只認這個 capability 的 `user_id`。
2. 節點第一次登入（不用 auth key）：在主機上以同一個 state volume 跑一次性容器，Will 核准它印出的網址：
   ```bash
   docker volume create bfx_tsstate
   docker run -d --name bfx-ts-login --read-only --cap-drop ALL --security-opt no-new-privileges:true \
     --tmpfs /tmp --tmpfs /var/run/tailscale -v bfx_tsstate:/var/lib/tailscale \
     --entrypoint tailscaled <docker-compose.app.yml 裡的 tailscale image> \
     --tun=userspace-networking --statedir=/var/lib/tailscale
   docker exec bfx-ts-login sh -c 'tailscale up --advertise-tags=tag:bfx-ops --hostname=bfx-ops --timeout=20m'
   ```
   不經 containerboot：它等登入有時限，會在核准前退出。`tailscale status` 顯示 tagged、無 key 到期後
   `docker rm -f bfx-ts-login`；之後的部署沿用這個 volume（`TS_AUTH_ONCE=true`），`tailscale.env` 可以是空檔。
3. `webapi.env` 寫好入口設定（§1e），部署。

驗證：從 tailnet 裡的 operator 裝置開 `https://<ops-host>/`（轉到 `/en/overview/`、讀取正常）；不在 tailnet 時
連不到；帶外站 `Origin` 的 POST 回 403。回滾：見 [deploy runbook](deploy.md) 的「Tailnet 入口」；bot 不受影響。

## 3. 首次安裝主機工具

本節安裝 `bfx-deploy`、告警與 DR units；不會建立 Postgres、runtime secrets、
帳戶資料或 pgBackRest repository。先完成資料服務與 `bfx_default` network、上面的
roles／連線設定，以及 [offsite DR](offsite-dr.md) 的備份與 restore 前置作業。
部署前須有可用的 restore test；空資料庫與缺少 verifier image 的主機不能直接套用
既有環境的部署流程。

### 3a. 安裝工具與 units

在 Linux VM 的乾淨 checkout `/home/ubuntu/bfx-funding-bot`，核對 HEAD 是要安裝的
release revision；CI 與 Release images 必須已成功。主機需要 root 擁有的
`/usr/local/bin/uv`（版本與 CI 的 `setup-uv` `version` 相同，從 GitHub release 下載 `uv-aarch64-unknown-linux-gnu.tar.gz` 並核對 `.sha256` 後安裝）、Docker buildx 與 Python 3.12 以上：

```bash
/usr/local/bin/uv --version
docker buildx version
/usr/bin/python3 --version
sudo /home/ubuntu/bfx-funding-bot/deploy/vm/ops/install.sh
ls -l /usr/local/lib/bfx-ops/current /home/ubuntu/bfx-releases/current
```

安裝器從 git 物件寫出 `/usr/local/lib/bfx-ops/releases/<rev>`，依 `ops/uv.lock`
執行 `uv sync --frozen`，建立 `bfx-deploy`／`bfx-notify`／`bfx-op` wrappers、
`/etc/sudoers.d/bfx-op`（sudo 只為 `bfx-op` 保留 `SSH_CONNECTION`，寫入前 `visudo -c` 驗證）與
`/home/ubuntu/bfx-releases/<rev>` DR checkout，再安裝 `systemd/managed-units`
列出的 units。它不會啟用或啟動任何 timer。之後工具由成功部署的 release 自動更新；
sbin 入口與 sudoers 例外，見 [deploy runbook §4](deploy.md#4-主機工具與-dr-腳本的版本)。

### 3b. Runtime 設定與首次驗證

- `/opt/bfx/runtime/{bot,webapi,tailscale,migrate,notify,ghcr}.env` 依
  [deploy env 規則](deploy.md#env-檔規則) 準備，root 擁有且 mode `0600`。
- bot 與 webapi 的七個秘密檔依 [deploy「秘密檔」](deploy.md#秘密檔botwebapi9h) 準備（`root:4600 0640`，不是空檔）：
  - 新主機沒有舊的 env 檔可以搬，每個值照 [secret-rotation「通用寫法」](secret-rotation.md#通用寫法) 在 root shell 裡產生並寫入（不上 argv）；
  - `BFX_VAULT_KEK` 用 `openssl rand -base64 32` 產生新值，再以 `bfx-op rotate-credential` 存一把 Bitfinex key
    （[secret-rotation](secret-rotation.md#vault-kekbfx_vault_kek)）；
  - 舊主機的 KEK 不沿用。
- mirror `/home/ubuntu/bfx-funding-bot` 以匿名 HTTPS clone（`https://github.com/Will413028/bfx-funding-bot.git`），
  不建 deploy key（[deploy「Mirror」](deploy.md#mirrorgit-fetch-走匿名-https9h)）。
  bot 與 `operator.env` 使用已核對的 canonical `BFX_EXCHANGE_ACCOUNT_ID`；不沿用舊 launcher 的 `BFX_RELEASE_*`。
- `tailscale.env` 放一把新的 tagged（`tag:bfx-ops`）、單次使用的 auth key；新主機的 `bfx_tsstate` 是空的，
  節點會重新登入（舊節點先在 admin console 刪掉，名稱 `bfx-ops` 才不會變成 `bfx-ops-1`）。
- 告警的 bot token 配置到 `notify.env` 的 `TELEGRAM_BOT_TOKEN` 與秘密檔 `secrets/bot/TELEGRAM_BOT_TOKEN`；chat id 配置到 `notify.env` 與
  `bot.env` 的 `TELEGRAM_CHAT_ID`；GHCR 憑證只需 `read:packages`。設定方式見 [operations](operations.md#6-告警)。
- error tracking 的 `SENTRY_DSN`（`bfx-backend`）同時配置到 `bot.env` 與 `webapi.env`；空值等於關閉。
  部署後依 [operations](operations.md#error-tracking-test) 各送一筆測試例外。

Restore test 不需要設定檔（ledger mode 驗證 restored copy 內的每個 scope），使用本機
`bfx-bot:local` 跑唯讀 boot check。先核對它對應的 image 與模組；缺少 image 或下列檢查
失敗時，先處理 verifier 來源，不可隨意重 tag：

```bash
sudo docker run --rm --pull=never --entrypoint "" bfx-bot:local /app/.venv/bin/python -c \
  "import bfx_funding_bot.apps.restore_boot_check, bfx_funding_bot.apps.authority_support, bfx_funding_bot.modules.ledger.wiring; print('ok')"
sudo systemctl start --no-block bfx-restore-test@current.service
journalctl -fu bfx-restore-test@current.service
```

確認該 service 成功，且 `/home/ubuntu/bfx/dr-evidence/restore-heartbeat.json` 是本次
產生的新 evidence。再執行 `sudo bfx-notify --level info "bfx setup: notify test"`，
確認告警送達；最後執行 `sudo bfx-deploy --dry-run`，核對 revision、CI run、
migration／restore 計畫、rollback target 與所有 blockers。

若出現 `foreign_container_holds_name:bfx-*`，表示同名 container 不屬於 `bfx-app`。
先記錄其 image、restart policy 與用途，安排停機並保留回復副本後才釋放名稱；
bfx-deploy 不會替 operator 刪除它們。

### 3c. 首次部署與排程

Dry run 無 blocker 後，以 systemd 執行，避免 SSH 斷線中斷部署：

```bash
sudo systemctl start --no-block bfx-deploy.service
journalctl -fu bfx-deploy.service
```

依 [deploy runbook](deploy.md) 核對 ledger 的 `started`／`deployed`、`bfx-app` 的各個 container
（bot、webapi、netns、tailscale）的 ownership 與 digest、health、工具與 DR checkout 的 `current` revision。
新資料庫在 migration 後完成 §1d 的隔離驗證；再從 tailnet 打開 UI（§2）驗證
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
