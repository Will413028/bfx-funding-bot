# Fresh host setup：DB roles 與 frontend 公開入口

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
