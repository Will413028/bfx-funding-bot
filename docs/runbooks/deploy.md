# Deploy runbook（CI → GHCR → bfx-deploy）

現行唯一的應用程式部署路徑。決策來源：ADR 2026-09-25
`ci-registry-digest-deploy`（供應鏈）與 `automated-probation-replaces-release-ceremony`
（分級與核准）。一次性的切換步驟見 [cutover runbook](cutover-release-governance.md)；
交易狀態、核准與停機操作見 [operations runbook](operations.md)。

舊的 immutable-release 流程（bundle、release session、canary permit、halt epoch、Halt 2
收據、`/opt/bfx/releases` 手動 load）與 `scripts/deploy-vm.sh` 已刪除，不要再照舊文件操作。

## 1. 流程總覽

```
push main ──► CI (.github/workflows/ci.yml) 綠燈
          ──► Release images (.github/workflows/release.yml)
                arm64 backend/frontend image → GHCR
                tags: sha-<40 位 revision>、main；label org.opencontainers.image.revision
          ──► VM bfx-deploy.timer（每 5 分鐘，*:2/5）→ bfx-deploy
```

- Image：`ghcr.io/will413028/bfx-funding-bot-backend`、`ghcr.io/will413028/bfx-funding-bot-frontend`。
  `main` tag 只用來「發現」最新 release；部署一律以 `repository@sha256:<digest>`。
- Frontend 的 `NEXT_PUBLIC_*` build 參數來自 `deploy/vm/frontend-public.json`（公開設定，
  CI 會檢查只有三個 key）。改它就是改 image，要走一次部署。
- VM 從不 build image。

### bfx-deploy 一次執行做什麼

1. `flock`（`/run/lock/bfx-deploy.lock`），兩個執行不會重疊。
2. 解析 GHCR `backend:main` 的 digest；若 ledger 最新的成功部署已是它就結束。
   **上次失敗或 rolled_back 的 digest 不會自動重試**，要 `--retry`。
3. 從 image label 讀 revision，要求 `backend:sha-<rev>` 是同一個 digest，並解析
   `frontend:sha-<rev>`。
4. 在 VM mirror（`/home/ubuntu/bfx-funding-bot`，以 `ubuntu` 身分跑 git）fetch，要求 `<rev>`
   在 `origin/main` 上；compose 檔、`live.env` 與分級規則都取自 `<rev>` 本身。
5. 分級（見 §2）。
6. 以 digest pull 兩個 image；檢查 `/opt/bfx/runtime/*.env`；在新 image 的 one-shot
   （`migrate.env`，owner role）比較 `alembic current` 與 `alembic heads`。
7. 有待套用的 migration：先 `deploy/vm/pgbackrest/backup.sh --type diff`（必須成功），
   再 isolated restore test（見 §4），再 `alembic upgrade head`。
   沒有 migration、但 diff 碰到 `deploy/vm/pgbackrest/**`、`deploy/vm/postgres/**`、
   `docker-compose.bot.yml`、`docker-compose.dr.yml`（或 diff 讀不到）時，也先跑 restore test。
8. `docker compose -p bfx-app -f <release>/deploy/vm/docker-compose.app.yml up -d --no-deps bot webapi frontend`，
   重新 inspect 每個 container 的 hardening（read-only rootfs、UID、cap-drop ALL、
   no-new-privileges、command、port、env 注入變數），等健康（bot `:8080/healthz`、
   webapi health、frontend `127.0.0.1:3001`），再 settle 60 秒。
9. 成功：`bfx-bot:local` 重新 tag 到新 backend（weekly report 與 DR verifier 仍用這個 alias），
   刪掉目前與上一版以外的舊 digest。
10. 每次嘗試寫一列 append-only `deployments` ledger，並發 Telegram
    （deployed＝info、有 warning 或 rolled_back＝warning、其他＝critical）。

PostgreSQL 與 Redis 在 `docker-compose.bot.yml`（project `bfx`），bfx-deploy 從不碰；
app 只加入既有的 `bfx_default` network。`docker-compose.bot.yml` 的 `legacy-app` profile
是歷史定義，**不要** `--profile legacy-app up`（它用同樣的 container name，會和 `bfx-app` 衝突）。

## 2. 變更分級（`deploy/change-class.yaml`）

- bfx-deploy 用**目標 commit** 的 `deploy/change-class.yaml` 分類
  `git diff --name-only --no-renames <已部署>..<目標>`。
- 路徑只有在符合 `standard` 且不符合任何 `material` 時才是 standard；其餘一律 material。
  一個 material 路徑就讓整個 release 是 material。
- 無法判斷（規則檔缺或壞、已部署 commit 不在歷史、diff 失敗、第一次部署）＝ material。
- 規則檔本身永遠是 material（寫死在 `change_class.py`），放寬規則需要一次核准。
- `--force-material` 只能升級，不能降級。

效果（詳見 [operations](operations.md#3-release-flow核准與限額期)）：

| 分級 | 開機後的交易狀態 |
|---|---|
| standard | 保持部署前的 trading state |
| material | 沒有該 digest 的核准 → `REDUCING/material_deploy`（原本 HALTED 就維持 HALTED），等 operator 在 UI 以 TOTP 核准一次 → ACTIVE 限額期 |

## 3. 常用指令（VM，root）

```bash
sudo bfx-deploy --dry-run          # 發現、fetch、分級、pull、比 schema；不改任何東西，輸出 JSON plan
sudo bfx-deploy                    # 手動跑一次（timer 平常自己跑）
sudo bfx-deploy --retry            # 重試上次 failed/rolled_back 的同一個 digest
sudo bfx-deploy --force-material   # 把這次 release 升為 material
systemctl list-timers 'bfx-*'
journalctl -u bfx-deploy.service -n 200 --no-pager
```

`--dry-run` 的 JSON 看 `change_class`、`class_detail`、`migrations_pending`、
`restore_test_before_deploy`、`rollback_target`、`blockers`（例如
`foreign_container_holds_name:bfx-bot`）。

部署紀錄（最新在上）：

```bash
docker exec --user postgres bfx-postgres psql -U bfx -d bfx -c \
  "SELECT id, finished_at, left(source_revision,12) rev, change_class, migrations_applied, outcome, detail
     FROM deployments ORDER BY id DESC LIMIT 10"
```

目前在跑的 digest 也可用 `docker inspect bfx-bot --format '{{.Image}}'` 或 UI 交易狀態面板的
「後端 image」確認。

### Env 檔規則

`/opt/bfx/runtime/{bot,webapi,frontend,migrate,notify,ghcr}.env`：root 擁有、mode `0600`、
上層目錄不可被他人寫入、不可是 symlink。bfx-deploy 啟動前會檢查，違反即 `precheck` 失敗：

- 每行 `KEY=VALUE`，不可重複 key；
- 交給 compose 的檔（bot/webapi/frontend 與 `live.env`）的值不可含 `$`、不可有包住的引號、
  不可含 ` #`（compose 會讀成別的東西）；
- 不可設定 `PYTHONPATH`／`PYTHONHOME`／`PYTHONUSERBASE`／`LD_PRELOAD`／`LD_LIBRARY_PATH`／
  `LD_AUDIT`／`NODE_OPTIONS`，也不可自己設 `BFX_IMAGE_DIGEST`／`BFX_SOURCE_REVISION`／
  `BFX_CHANGE_CLASS`（由 bfx-deploy 注入）。
- `ghcr.env`：`GHCR_USERNAME`、`GHCR_TOKEN`（read-only `read:packages`）；檔案不存在＝匿名 pull。
- `notify.env`：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`（見 [operations §6](operations.md#6-告警)）。

## 4. 部署中的備份與 restore test

- 有 migration 一定先備份；備份失敗 → `failed`，migration 不套用，現行 release 不動。
- restore test 由 bfx-deploy `systemctl start bfx-restore-test.service` 同步等結果；
  失敗 → `failed`（`restore_test_failed(<reason>)`），什麼都不部署。
- bfx-deploy **不會**更新 VM checkout 裡的 DR 腳本（`deploy/vm/pgbackrest/*`，pgBackRest timer
  直接執行它們）。DR 腳本的變更要手動 pull 到 VM checkout，再手動跑一次 restore test；
  細節見 [offsite DR](offsite-dr.md#monthly-and-change-triggered-prefix-restore-test)。

## 5. 失敗與回滾

| ledger outcome | 意思 | 要做什麼 |
|---|---|---|
| `deployed` | 成功 | material 的話到 UI 核准（見 operations） |
| `failed`，detail `precheck:*` | 部署前檢查失敗，現行 release 沒動 | 依 code 修（env、registry、git、foreign container…），再 `--retry` |
| `failed`，`backup_failed` / `restore_test_failed` | migration 未套用，現行 release 沒動 | 看 `journalctl -u bfx-restore-test.service` 或 backup log，修好後 `--retry` |
| `failed`，`migration_failed` | migration 失敗，schema 未變 | 修 migration 出新 release |
| `failed`，`migration_partial_or_unverified` | schema **已變**、狀態不明 | bot 不會以舊 code 起來；見下方「migration 之後」 |
| `rolled_back` | 新版不健康、沒有 migration，已回到上一個成功的 digest | 查原因；修好出新 release，或 `--retry` 同一 digest |
| `failed`，`unhealthy:*; migrations applied` 或 `no previous deployment` | 不能自動回滾，bfx-deploy 已停 bot | 見下方 |

**migration 之前**失敗：現行 release 不受影響；自動回滾只會發生在沒有 migration 的情況。

**migration 之後**失敗：舊 code 可能不能在新 schema 上跑（live daemon 開機會比對
image 內 migrations 推導出的唯一 head（`core/schema_head.build_head`）與 `alembic_version`，不一致就寫 `HALTED/auto`、盡力
cancel-all、拒絕開機）。只能：

1. roll forward：修正後出新 release（新 commit → CI → GHCR → bfx-deploy）；或
2. 把 migration 前的備份還原到**隔離**資料庫比對，再決定。**不要把舊備份蓋回 production**
   ——venue 可能已有寫入，見 [rollback after a venue write](rollback-after-venue-write.md)。

bot 停下時 venue 上的掛單不會自己消失：先在 UI 或 `/admin/halt` 執行 kill（見 operations）。
bot 停著時兩者都不可用，改在 Bitfinex 網頁手動撤單並記錄。

## 6. Rollback drill（刻意走一次回滾路徑）

需要：已有至少一次成功部署、且這次 release 沒有待套用 migration（否則會被拒絕）。

```bash
sudo systemctl stop bfx-deploy.timer
sudo bfx-deploy --rollback-drill   # 部署新 release，健康後故意走回滾 → ledger rolled_back
sudo bfx-deploy --retry            # 真的部署同一個 digest
sudo systemctl start bfx-deploy.timer
```

驗收：ledger 依序有 `rolled_back`（detail `unhealthy:rollback_drill; rolled_back_to=<上一版>`）
與 `deployed`；Telegram 收到 warning 與 info；兩次之間 `docker inspect bfx-bot` 顯示的是上一版 digest。

## 7. 暫停自動部署

```bash
sudo systemctl stop bfx-deploy.timer      # 暫停；手動 bfx-deploy 仍可用
sudo systemctl start bfx-deploy.timer     # 恢復
```

`bfx-deploy.service` 的 `TimeoutStartSec=6h`（備份＋restore test＋pull＋migration 的最壞情況），
systemd 不會在 migration 中途把它砍掉。不要手動 kill 正在跑 migration 的 bfx-deploy。
