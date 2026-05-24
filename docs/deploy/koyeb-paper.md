# Koyeb Paper/Shadow Deploy Runbook (Phase 4.1)

> 用途：BFX_PHASE=paper 1hr smoke → L3 smoke PASS → BFX_PHASE=shadow 2-4 週 run。
> 範圍：Phase 4.1。Phase 4.2 加入 real-money execution 後另寫 canary runbook。
> Spec: [phase4.1-koyeb-deploy-design.md](../superpowers/specs/2026-05-18-phase4.1-koyeb-deploy-design.md)

## TL;DR — `scripts/deploy-koyeb.sh`

第一次以後的 deploy 不用看 Prerequisites / Koyeb Service 設定段。直接：

```bash
./scripts/deploy-koyeb.sh paper      # 起 paper 1hr smoke
./scripts/deploy-koyeb.sh shadow     # 切到 shadow（無自動退時間）
```

Script 是 idempotent：app/service 缺則建、env vars 已存在則更新、每次都會 trigger 新一次 deploy。Koyeb 自動拉 main branch 最新 commit。

**第一次** 部署需要的 manual 設定（dashboard 操作，script 不做）見下方 Prerequisites + Koyeb Service 設定。

## Prerequisites

部署前確認以下都備齊：

| 項目 | 怎麼確認 |
|---|---|
| Koyeb account + GitHub repo connected | https://app.koyeb.com → Apps → 看到 `bfx-funding-bot` 或可建立新 app 連 GitHub repo |
| Neon Postgres project + DATABASE_URL | https://console.neon.tech → 你的 project → Connection Details → 複製 connection string |
| Upstash Redis (optional) | https://console.upstash.com → Database → 複製 `rediss://...` URL |

## Env Vars

在 Koyeb dashboard 的 service settings → Environment 區設定：

| Env Var | Required | Paper 值 | Shadow 值 | 備註 |
|---|---|---|---|---|
| `BFX_PHASE` | ✅ | `paper` | `shadow` | 字面值。`canary` 會被 daemon reject |
| `DATABASE_URL` | ✅ | `postgresql+asyncpg://...?sslmode=require` | 同 paper（共用 schema） | ⚠️ **必須用 `postgresql+asyncpg://` scheme，不是 Neon 預設給的 `postgresql://`**。詳見下方注意點 |
| `REDIS_URL` | optional | `rediss://default:...@host:6379` | 同 paper | Upstash。可暫不設 |
| `BFX_RUN_DURATION_HOURS` | optional | `1` | **不設 / 刪除** | Paper 1hr 自動退；Shadow flip 必須刪掉這個 var |

⚠️ **不要設** `BFX_CELLS_YAML` — image 已內建 `/app/configs/cells.yaml`，Koyeb 重設反而會 override。

### `DATABASE_URL` 注意點（Phase 4.2 fix 之前必讀）

Neon dashboard 給的 connection string 預設長這樣：

```
postgresql://user:pass@ep-xxx-pooler.<region>.aws.neon.tech/dbname?sslmode=require&channel_binding=require
```

**三處要手動改**才能跟 asyncpg 相容：

1. **Scheme**：`postgresql://` → `postgresql+asyncpg://`
2. **`sslmode=require`** → `ssl=require`（asyncpg 用 `ssl=`，不認識 `sslmode=`）
3. **移除** `&channel_binding=require`（asyncpg 不支援 channel binding 協商）
4. **建議移除** `-pooler` 後綴：用 Neon direct endpoint 而非 pooler，避免 asyncpg prepared statements 與 PgBouncer transaction-mode 衝突（migrations 必走 direct）

最終 URL：

```
postgresql+asyncpg://user:pass@ep-xxx.<region>.aws.neon.tech/dbname?ssl=require
```

**為什麼這麼多手腳**：SQLAlchemy 的 asyncpg dialect 把 URL query params 當 keyword 傳給 `asyncpg.connect()`，但 asyncpg 的 API 與 libpq 不同（`ssl=` vs `sslmode=`，不支援 `channel_binding`）。Neon dashboard 給的是 libpq-style URL，asyncpg 看不懂。

**Phase 4.2 修法**：`core/settings.py` 在 `database_url` accessor 自動做上述 4 處 transform，到時 user 可直接貼 Neon 原 URL，不用手改。詳見 spec [code-debt 段](../superpowers/specs/2026-05-18-phase4.1-koyeb-deploy-design.md)。

## Koyeb Service 設定

第一次部署時建 service：

| 欄位 | 值 |
|---|---|
| Service type | **Web**（自 2026-05-21 起：daemon 內建 healthz uvicorn 於 port 8080；Koyeb worker 不支援 health check，故走 web 但 port 設 TCP 維持 mesh-internal） |
| GitHub repo | `<your-account>/bfx-funding-bot` |
| Branch | `main` |
| Build method | **Dockerfile** |
| Dockerfile location | `backend_py/Dockerfile` |
| Build context | `backend_py/` |
| Instance type | `nano`（1hr paper 跑得動）或 `micro`（shadow 長跑可選） |
| Region | `sin`（Singapore，與 Neon `ap-southeast-1` 同區，RTT < 5ms；若 Neon project 在別區改對應 Koyeb region） |
| Auto-deploy on push | ✅ enabled |
| Port | `8080:tcp`（TCP protocol → 不會被 auto-create public route，mesh-internal only） |
| Health check | HTTP `:8080/healthz`，grace period 90s（daemon 跑 alembic upgrade + warmup 約需 60s，多留 buffer） |
| Routes | 無（明確刪：`--routes '!/'`）。若用 `port:http` 會被 Koyeb auto-create 公開 route → 暴露 `/healthz` 到 public URL `<app>-<user>-<hash>.koyeb.app/healthz` |

按 **Deploy** 觸發第一次 build。

> **2026-05-21 transition note**：原 service type 為 Worker（Phase 4.1 daemon 無 HTTP）。Phase 4.2.0 加入 healthz uvicorn 後，Worker 改 Web 並走「TCP port + HTTP health check + no route」組合（Koyeb 支援 HTTP probe on TCP port）。CLI 切換命令：
>
> ```bash
> koyeb service update bfx-funding-bot/marketfeed \
>   --type web \
>   --ports 8080:tcp \
>   --checks 8080:http:/healthz \
>   --checks-grace-period 8080=90 \
>   --routes '!/'
> ```
>
> 注意：若把 `--type web` 跟 `--routes '!/'` 同 command 跑，第一次切 web type 時 Koyeb 還沒 auto-create route，`!/` flag 被 ignore → 切完 type 後 Koyeb 才補建 route → 中間有 window 暴露 `/healthz`。建議直接用 `--ports 8080:tcp` 避開 auto-route 機制。

## 部署流程

1. **Code change** → 本機 commit + push
   ```bash
   git push origin main
   ```
2. **Koyeb auto-builds** → dashboard `Activity` 看 build log
3. **Build success** → service 自動 start → `Runtime logs` 看 daemon 啟動
4. **首 30s 預期看到**：
   - `alembic upgrade head` output（已 up to date 或套用 migration）
   - `daemon_started phase=paper cells=11`（或 shadow）
5. **5 分鐘內預期在 Koyeb Runtime logs 看到 structured stdout event**：
   - 應出現 `phase=paper` 的 candle / signal / health 結構化 JSON 行（signal events 直接寫 stdout，無 Axiom）

## Deploy Verification（L3 Smoke Endpoint）

Paper 跑完（Koyeb runtime log 看到 `daemon_run_duration_reached hours=1`）後，在切 shadow 前執行 L3 smoke 驗證。

**Signal events 現在直接寫 structured stdout**（Koyeb Runtime logs 可查），無需外部 Axiom。

### L3 Smoke 執行方式

```bash
curl -X POST "https://<your-service-url>/smoke-test?level=L3" \
  -H "Authorization: Bearer <admin-token>"
```

L3 smoke 會在 PG event_log 上執行 read-your-writes 驗證，確認完整執行鏈（execution chain）正常：
- `RESERVATION_CLAIMED` event 已落地
- `ORDER_FILL` event 已落地

回傳 `{"status": "ok", "level": "L3"}` 代表 PASS；任何非 2xx 或 `"status": "fail"` 代表失敗。

Container exit code 行為：

| Exit | 意義 | Koyeb 行為 |
|---|---|---|
| 0 | Daemon 正常退（paper duration 到） | Container 結束 |
| 1 | Config / runtime fatal | Deploy 標 unhealthy，預設 restart loop — **先查 runtime log 找 fail 原因** |

⚠️ **L3 smoke 不 PASS 不要切 shadow**。先排 issue 再重跑 1hr paper。

## Shadow Flip（L3 PASS 後）

1. Koyeb dashboard → service Settings → Environment
2. **改** `BFX_PHASE` 從 `paper` → `shadow`
3. **刪** `BFX_RUN_DURATION_HOURS`（重要 — 不刪會 1hr 後又自退）
4. （可選）改 instance type 從 `nano` → `micro`，shadow 長跑穩定性更高
5. Save → service 自動 redeploy
6. Runtime log 應該看到 `daemon_started phase=shadow cells=11`，且 daemon 持續跑（沒有 `daemon_run_duration_reached`）

Shadow 預期跑 2-4 週累積數據，給 Phase 4.3 calibration 用。

## Troubleshooting

| 症狀 | 可能原因 | 處置 |
|---|---|---|
| Build fail：`Could not find a version that satisfies the requirement` | numpy/pandas py3.13 wheel 未發布 | 等 wheel 發布或暫降 Python 版本（最後手段） |
| Build fail：`uv sync` lock mismatch | `uv.lock` 過時 | 本機 `cd backend_py && uv lock` 重產，commit, push |
| Container 啟動 → `ModuleNotFoundError: psycopg2` | `DATABASE_URL` 用了 `postgresql://` scheme | 改成 `postgresql+asyncpg://`（見上方 DATABASE_URL 注意點） |
| Container 啟動 → `TypeError: connect() got an unexpected keyword argument 'sslmode'` | `DATABASE_URL` 含 `sslmode=require`，asyncpg 不認識 | 改成 `?ssl=require`（見上方 DATABASE_URL 注意點） |
| Container 啟動 → asyncpg 報 channel binding 相關錯 | `DATABASE_URL` 含 `&channel_binding=require` | 移除這個 query param |
| Container 啟動 → 連 Neon 卡住或 prepared statement 錯 | `DATABASE_URL` hostname 帶 `-pooler` 後綴 | 用 Neon direct endpoint（移除 `-pooler`） |
| Container 啟動 → `alembic upgrade head` fail（網路相關） | Neon 連線失敗 / DNS / IP allowlist | 本機 `cd backend_py && DATABASE_URL=<prod> uv run alembic upgrade head` 抓真實錯 |
| Container 啟動 → `alembic upgrade head` fail（schema 相關） | migration conflict / 權限不足 | 看 alembic 訊息；常見 `ProgrammingError: permission denied` → Neon role 缺 `CREATE` 權限 |
| `daemon_run_duration_reached` 在 shadow 也觸發 | 忘記刪 `BFX_RUN_DURATION_HOURS` | 回 Env vars 刪掉這個，save → redeploy |
| L3 smoke 回 `{"status": "fail"}` 或非 2xx | PG event_log 缺少 RESERVATION_CLAIMED / ORDER_FILL event，execution chain 斷 | 查 Koyeb runtime logs 的 structured stdout，找對應 signal / fill log 行；確認 `DATABASE_URL` 正確且 migration 已套用 |
| Daemon exit code 1 with `config_fatal` | env var 漏設 / 值錯 | 看 log message，補設 |
| Koyeb worker SIGTERM 後沒退 | grace period 不夠 / daemon hang | Koyeb default 60s 通常夠，若不夠加大 Koyeb instance `Graceful Shutdown` 設定 |
| Daemon 啟動報 `channel binding negotiation failed` | Neon `channel_binding=require` 與 asyncpg 版本不相容 | 從 DATABASE_URL 拿掉 `channel_binding=require`，只留 `sslmode=require` |

## Rollback

| 場景 | 怎麼回 |
|---|---|
| Dockerfile / 程式碼變更導致 fail | 本機 `git revert <bad-commit>` → push → Koyeb 自動 build 回上一版 |
| Env var 改錯 | dashboard 改回正確值，save → redeploy |
| Shadow flip 後想退回 paper | 改 `BFX_PHASE=shadow` 回 `paper`，重設 `BFX_RUN_DURATION_HOURS=1`，save → redeploy |

## 已知限制 / Phase 4.2 code-debt

部署本 phase 過程中發現以下需在 Phase 4.2 治本的問題（runbook 已 workaround 但建議盡早消除）：

1. **`modules/marketfeed/config.py:127` 預設 cells.yaml 路徑** — `load_config()`
   無 `cells_yaml_path` 參數 + 無 `BFX_CELLS_YAML` env → `Path(__file__).parents[4]`
   在 site-packages 場景錯。
   - Phase 4.1 workaround: image-baked `ENV BFX_CELLS_YAML=/app/configs/cells.yaml`
   - **Fixed in Phase 4.2.0 D1**：lookup precedence chain (env / cwd / importlib.resources)。
     image env 仍保留作為 explicit deploy-time default，但 cwd / importlib fallback
     讓 local dev / packaged install 也 works。
2. **DATABASE_URL scheme auto-transform** — `core/settings.py` 應自動把 `postgresql://` 轉成 `postgresql+asyncpg://`，免去 user 手動改 scheme。
3. **numpy/pandas 分組** — 已於 deploy commit 修正（移到 `[project] dependencies`），但 Phase 4.1 22-task 階段未抓到此 layout 錯誤，需檢視 module boundary 是否還有類似情況。
