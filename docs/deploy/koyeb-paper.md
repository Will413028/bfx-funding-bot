# Koyeb Paper/Shadow Deploy Runbook (Phase 4.1)

> 用途：BFX_PHASE=paper 1hr smoke → G1 PASS → BFX_PHASE=shadow 2-4 週 run。
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
| Axiom dataset `bfx-funding-bot` 已建 | https://app.axiom.co → Datasets → 看到 `bfx-funding-bot` |
| Axiom API token（ingest + query scope） | Axiom Settings → API Tokens → 新建一把（**只記在密碼管理器 / Koyeb，不貼進 chat / git**） |

## Env Vars

在 Koyeb dashboard 的 service settings → Environment 區設定：

| Env Var | Required | Paper 值 | Shadow 值 | 備註 |
|---|---|---|---|---|
| `BFX_PHASE` | ✅ | `paper` | `shadow` | 字面值。`canary` 會被 daemon reject |
| `AXIOM_API_KEY` | ✅ | `xaat-...` | 同 paper | 直接貼進 Koyeb dashboard，不經 chat / repo |
| `AXIOM_DATASET` | ✅ | `bfx-funding-bot` | 同 paper | Axiom dashboard 上實際 dataset 名 |
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
| Service type | **Worker**（不是 Web Service — daemon 無 HTTP port） |
| GitHub repo | `<your-account>/bfx-funding-bot` |
| Branch | `main` |
| Build method | **Dockerfile** |
| Dockerfile location | `backend_py/Dockerfile` |
| Build context | `backend_py/` |
| Instance type | `nano`（1hr paper 跑得動）或 `micro`（shadow 長跑可選） |
| Region | `sin`（Singapore，與 Neon `ap-southeast-1` 同區，RTT < 5ms；若 Neon project 在別區改對應 Koyeb region） |
| Auto-deploy on push | ✅ enabled |
| Health check | （worker 無 HTTP，跳過） |

按 **Deploy** 觸發第一次 build。

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
5. **5 分鐘內預期 Axiom 看到 event**：
   - https://app.axiom.co → `bfx-funding-bot` dataset → Live Stream
   - 應出現 `phase=paper` 的 candle / signal / health event

## G1 Self-Smoke Trigger（自動）

從 [Phase 4.1.x C1 redesign](../superpowers/specs/2026-05-21-g1-c1-continuity-redesign-design.md) 起，
daemon 在 `BFX_PHASE=paper` + `BFX_RUN_DURATION_HOURS` 設定下，
跑完 duration 後**自動執行 G1 smoke**：

1. daemon `_run()` finally block 完成（Axiom flush + http client close）
2. 等 30 秒讓 Axiom ingestion 完成 catch-up
3. 跑完整 C1-C6 check
4. Container exit code = G1 exit code

Container exit code 行為：

| Exit | 意義 | Koyeb 行為 |
|---|---|---|
| 0 | Daemon + G1 全 pass | Deploy 標 healthy，container 結束 |
| 1 | G1 fail（某 check fail） | Deploy 標 unhealthy，預設 restart loop（**feature** — 強制查 log） |
| 2 | Axiom auth/network fail | 同 1，cause 在 config 而非 daemon |
| 3 | Config error（env vars 漏設 / EDA range mismatch） | 同 1，cause 在 config |

⚠️ 若 G1 fail 進 restart loop，**先查 runtime log 找 fail 原因再放著 restart**，否則 burn quota 跑無效 cycle。

## G1 Smoke 手動驗證（debug 用）

> 通常**不需要手動跑** — 上方 Self-Smoke Trigger 已自動執行。本段用於：debug 失敗原因、重跑特定 check（`--only C1`）、或對歷史 window 跑 retroactive 檢查。

等 paper container 因 `BFX_RUN_DURATION_HOURS=1` 自動退（Koyeb runtime log 看到 `daemon_run_duration_reached hours=1`）。

本機跑：

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
export AXIOM_API_KEY=<your-token>  # 同 Koyeb 那把
export AXIOM_DATASET=bfx-funding-bot
uv run python scripts/g1_smoke_check.py --hours 1
```

Exit code 解讀：

| Exit | 意義 | 處置 |
|---|---|---|
| 0 | All G1 checks PASS | 可進 shadow flip |
| 1 | 某 check fail | 看 stderr 訊息，常見 C1 continuity / C2 emit completeness / C3 range conformance fail — 通常是 cells.yaml 參數或 daemon 邏輯問題 |
| 2 | Axiom 連不上 | `AXIOM_API_KEY` 錯 / network / API quota |
| 3 | Config error | EDA range / cells.yaml 不符 — 看 `scripts/g1_smoke_eda_ranges.py` 與當前 cells.yaml 是否一致 |

⚠️ **G1 不 PASS 不要切 shadow**。先在 paper 排 issue 再重跑 1hr smoke。

## Shadow Flip（G1 PASS 後）

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
| Axiom 沒收到 event | `AXIOM_API_KEY` typo / token scope 不夠 / dataset 不存在 / quota 滿 | dashboard 重產 token（勾 ingest + query），重貼 Koyeb |
| Daemon exit code 1 with `axiom_auth_fail` | `AXIOM_API_KEY` 錯 | 同上 |
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
