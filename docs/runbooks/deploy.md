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
              含 compose hardening policy（docker compose config → compose_policy.py）
          ──► Release images (.github/workflows/release.yml)
                arm64 backend/frontend image → GHCR
                tags: sha-<40 位 revision>、main
                labels: org.opencontainers.image.revision、bfx.ci-run-url（通過的 CI run）
          ──► VM bfx-deploy.timer（每 5 分鐘，*:2/5）→ bfx-deploy
```

- Image：`ghcr.io/will413028/bfx-funding-bot-backend`、`ghcr.io/will413028/bfx-funding-bot-frontend`。
  `main` tag 只用來「發現」最新 release；部署一律以 `repository@sha256:<digest>`。
- Registry 查詢用 `docker buildx imagetools inspect`（GHCR 憑證只放在每次呼叫的暫時
  `DOCKER_CONFIG`，不留下 `docker login`）。
- Frontend 的 `NEXT_PUBLIC_*` build 參數來自 `deploy/vm/frontend-public.json`（公開設定，
  CI 會檢查只有三個 key）。改它就是改 image，要走一次部署。
- VM 從不 build image。

### bfx-deploy 一次執行做什麼

1. `flock`（`/run/lock/bfx-deploy.lock`），兩個執行不會重疊。
2. 解析 GHCR `backend:main` 的 digest；若 ledger 最新的成功部署已是它就結束。
   **上次失敗、rolled_back 或中斷（只有 `started` 列）的 digest 不會自動重試**，要 `--retry`。
3. 讀 image label 的 revision 與 CI run，要求 `backend:sha-<rev>` 是同一個 digest、
   `frontend:sha-<rev>` 帶同一個 revision。
4. 在 VM mirror（`/home/ubuntu/bfx-funding-bot`，以 `ubuntu` 身分跑 git）fetch，要求 `<rev>`
   在 `origin/main` 上；compose 檔、`live.env` 與分級規則都取自 `<rev>` 本身（git 物件，不讀 working tree）。
5. 分級（見 §2）。
6. 以 digest pull 兩個 image；檢查 `/opt/bfx/runtime/*.env`；在新 image 的 one-shot
   （`migrate.env`，owner role）比較 `alembic current` 與 `alembic heads`。
7. **有待套用的 migration**（Will 2026-09-25）：先停 bot（唯一的 writer）→
   用 `<rev>` 的 `backup.sh --type diff` 備份 → 用 `<rev>` 的 DR 腳本跑 isolated restore test
   （`bfx-restore-test@<rev>.service`）→ `alembic upgrade head`。**從停 bot 起任何一步失敗，
   bot 都維持停止**，只能 roll forward（新 release）或 `--retry`。
   **沒有 migration 但 diff 碰到 DR 路徑**（`deploy/vm/pgbackrest/**`、`deploy/vm/postgres/**`、
   `docker-compose.bot.yml`、`docker-compose.dr.yml`，或 diff 讀不到）：bot 照跑，先跑 restore test。
8. 寫入 ledger 的 `started` 列（attempt id、digest、revision、分級、CI run；之後不可修改），
   再 `docker compose -p bfx-app -f <release>/docker-compose.app.yml up -d --no-deps bot webapi frontend`。
   bot 開機時用注入的 `BFX_DEPLOYMENT_ID` 讀到自己這次部署的列。
9. 檢查每個 container 跑的是這次記錄的 digest 與身分 env（`BFX_IMAGE_DIGEST`、
   `BFX_SOURCE_REVISION`、`BFX_CHANGE_CLASS`、`BFX_DEPLOYMENT_ID`）、屬於 compose project `bfx-app`；
   等健康（bot `:8080/healthz`、webapi health、frontend `127.0.0.1:3001`），再 settle 60 秒。
   Hardening（read-only、UID、cap-drop、port、network）不在 VM 上重驗，而是 CI 對
   `docker compose config` 的輸出做 policy 檢查（`deploy/vm/ops/compose_policy.py`）。
10. 成功：`bfx-bot:local` 重新 tag 到新 backend（weekly report 與 DR verifier 仍用這個 alias）；
    安裝 `<rev>` 的主機工具與 systemd unit（見 §4，**下一輪才生效**）；DR checkout 的 `current`
    指到 `<rev>`；刪掉目前與上一版以外的舊 digest、工具版本與 DR checkout。
11. 寫入 ledger 的結束列（`deployed`／`rolled_back`／`failed`，與 `started` 列同一個 attempt id），
    並發 Telegram（deployed＝info、有 warning 或 rolled_back＝warning、其他＝critical）。
    在變更任何 container 之前就失敗的嘗試只有結束列。

PostgreSQL 與 Redis 在 `docker-compose.bot.yml`（project `bfx`），bfx-deploy 從不碰；
app 只加入既有的 `bfx_default` network。`docker-compose.bot.yml` 的 `legacy-app` profile
是歷史定義，**不要** `--profile legacy-app up`（它用同樣的 container name，會和 `bfx-app` 衝突）。

## 2. 變更分級（`deploy/change-class.yaml`）

- bfx-deploy 用**目標 commit** 的 `deploy/change-class.yaml` 分類
  `git diff --name-only --no-renames <上一次成功部署>..<目標>`（兩兩比較；把「上次被接受之後」
  所有 material 累積起來的是 bot 端，它讀 ledger 的 `started` 列）。
- 規則以 PyYAML 讀、pathspec（gitignore 語法、錨定在 repo 根目錄）比對；版本釘在
  `deploy/vm/ops/uv.lock`。
- 路徑只有在符合 `standard` 且不符合任何 `material` 時才是 standard；其餘一律 material。
  一個 material 路徑就讓整個 release 是 material。
- 無法判斷（規則檔缺或壞、已部署 commit 不在歷史、diff 失敗、第一次部署）＝ material。
- 規則檔本身永遠是 material（寫死在 `change_class.py`），放寬規則需要一次核准。
- `--force-material` 只能升級，不能降級。
- `BFX_CHANGE_CLASS` env 仍然注入，但只是輔助；權威是 ledger。

效果（詳見 [operations](operations.md#3-release-flow核准與限額期)）：

| 分級 | 開機後的交易狀態 |
|---|---|
| standard | 保持部署前的 trading state |
| material | 沒有該 digest 的核准 → `REDUCING/material_deploy`（原本 HALTED 就維持 HALTED），等 operator 在 UI 以 TOTP 核准一次 → ACTIVE 限額期 |

## 3. 常用指令（VM，root）

```bash
sudo bfx-deploy --dry-run            # 發現、fetch、分級、pull、比 schema；不改任何東西，輸出 JSON plan
sudo bfx-deploy                      # 手動跑一次（timer 平常自己跑）
sudo bfx-deploy --retry              # 重試上次 failed/rolled_back/中斷的同一個 digest
sudo bfx-deploy --force-material     # 把這次 release 升為 material
sudo bfx-deploy --recreate           # 同一個 release 重建 container（改了 runtime env 之後）
sudo bfx-deploy --recreate --dry-run # 預覽 recreate
systemctl list-timers 'bfx-*'
journalctl -u bfx-deploy.service -n 200 --no-pager
```

`--dry-run` 的 JSON 看 `change_class`、`class_detail`、`ci_run`、`migrations_pending`、
`stop_bot_before_migration`、`restore_test_before_deploy`、`rollback_target`、`blockers`
（例如 `foreign_container_holds_name:bfx-bot`）。

部署紀錄（最新在上；同一次嘗試的 `started` 與結束列共用 `attempt_id`）：

```bash
docker exec --user postgres bfx-postgres psql -U bfx -d bfx -c \
  "SELECT id, attempt_id, outcome, left(source_revision,12) rev, change_class, migrations_applied,
          finished_at, detail, ci_run
     FROM deployments ORDER BY id DESC LIMIT 10"
```

目前在跑的 digest 也可用 `docker inspect bfx-bot --format '{{.Config.Image}}'` 或 UI 交易狀態面板的
「後端 image」確認。

### `--recreate`（改了 runtime env 之後）

只改 `/opt/bfx/runtime/*.env` 不會觸發部署。`--recreate` 用 ledger 最新 `deployed` 列的
digest 與 revision，走同樣的 env 檢查、`started` 列、`--force-recreate`、身分檢查、健康等待與
結束列。分級預設 standard（程式碼沒有變）；env 的改動需要一次核准時加 `--force-material`。
recreate 沒有 rollback（舊 env 已經不在了）：不健康就停 bot、記 `failed`，修好 env 再 `--recreate`。
schema 不在該 release 的 head 時拒絕。

### Env 檔規則

`/opt/bfx/runtime/{bot,webapi,frontend,migrate,notify,ghcr}.env`：root 擁有、mode `0600`、
不可是 symlink（`ubuntu` 使用者的 checkout 由 DR unit 執行，secret 不能讓它讀到）。
bfx-deploy 啟動前會檢查，違反即 `precheck` 失敗：

- 每行 `KEY=VALUE`，不可重複 key；
- 交給 compose 的檔（bot/webapi/frontend 與 `live.env`）的值不可含 `$`、不可有包住的引號、
  不可含 ` #`：這些 secret 當初是為 `docker --env-file`（字面值）寫的，compose 的 env_file
  會做內插、去引號、截註解，值會默默變成另一個 secret；
- 身分 env（`BFX_IMAGE_DIGEST` 等）由 compose 的 `environment:` 注入，優先於 env 檔，寫在 env 檔
  也不會生效；
- `ghcr.env`：`GHCR_USERNAME`、`GHCR_TOKEN`（read-only `read:packages`）；檔案不存在＝匿名 pull。
- `notify.env`：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`（見 [operations §6](operations.md#6-告警)）。

## 4. 主機工具與 DR 腳本的版本

bfx-deploy 自己維護主機上的工具，不再依賴手動 `install.sh`（它只用於第一次安裝）：

| 路徑 | 內容 | 何時更新 |
|---|---|---|
| `/usr/local/lib/bfx-ops/releases/<rev>/{ops,systemd}` | 從 `<rev>` 的 git 物件寫出，root 擁有 | release 部署成功後 |
| `/usr/local/lib/bfx-ops/releases/<rev>/ops/.venv` | `uv sync --frozen`（`deploy/vm/ops/uv.lock`：PyYAML、pathspec） | 同上 |
| `/usr/local/lib/bfx-ops/current` → `releases/<rev>` | wrapper `bfx-deploy`／`bfx-notify` 與 unit 都執行這裡 | 同上，**下一輪 bfx-deploy 才用新版** |
| `/etc/systemd/system/<deploy/vm/systemd/managed-units>` | 複製後 `daemon-reload`，從不 enable/start/stop | 同上 |
| `/home/ubuntu/bfx-releases/<rev>` | mirror 的乾淨 git worktree（`ubuntu` 擁有），DR 腳本與 `docker-compose.dr.yml` | 需要備份或 restore test 時先建目標版本 |
| `/home/ubuntu/bfx-releases/current` → `<rev>` | 排程備份／status／每月 restore test 用 | release 部署成功後 |

- 選「下一輪生效」而不是「自我更新後繼續」：正在跑的 deployer 不在中途換程式碼，
  而且沒部署成功的 release 的工具永遠不會被裝上。代價是改 bfx-deploy 本身的 release，
  要到下一次部署才用上新邏輯（它自己的部署仍由舊版完成）。
- DR 腳本則是**這一次就用目標版本**：部署前的備份與 restore test 跑 `bfx-releases/<rev>` 裡的腳本，
  所以改 DR 腳本的 release 在上線前就被自己的腳本驗證過。
- 工具安裝失敗（例如 `uv sync` 連不到 PyPI）只是 warning：部署仍成功，`current` 留在舊版，
  下一次成功部署會再裝。
- 只保留目前與上一版的工具版本和 DR checkout。

## 5. 失敗與回滾

| ledger outcome | 意思 | 要做什麼 |
|---|---|---|
| `deployed` | 成功 | material 的話到 UI 核准（見 operations） |
| `failed`，detail `precheck:*` | 部署前檢查失敗，現行 release 沒動 | 依 code 修（env、registry、git、foreign container…），再 `--retry` |
| `failed`，`dr_checkout_failed` | 建不出目標的 DR checkout，現行 release 沒動 | 查 mirror／`/home/ubuntu/bfx-releases` 權限，再 `--retry` |
| `failed`，`bot_stop_failed` | 有 migration 但停不了 bot，什麼都沒做 | 查 docker，再 `--retry` |
| `failed`，`backup_failed` / `restore_test_failed(migration_pending)` / `migration_failed`，detail 含 `bot stopped until a release deploys` | schema 未變，**bot 已停** | 看 `journalctl -u 'bfx-restore-test@*'` 或 backup log；修好後 `--retry` 或出新 release |
| `failed`，`restore_test_failed(dr_paths:…)` | 沒有 migration 的 DR 變更沒通過 restore test，現行 release 沒動 | 修 DR 變更出新 release |
| `failed`，`migration_partial_or_unverified` | schema **已變**、狀態不明，bot 已停 | 見下方「migration 之後」 |
| `failed`，`ledger_started_unrecorded` | `started` 列寫不進去，container 沒動（有 migration 時 bot 已停） | 查 postgres，再 `--retry` |
| `rolled_back` | 新版不健康、沒有 migration，已回到上一個成功的 digest（用它原本的 deployment id） | 查原因；修好出新 release，或 `--retry` 同一 digest |
| `failed`，`unhealthy:*; migrations applied` 或 `no previous deployment` | 不能自動回滾，bfx-deploy 已停 bot | 見下方 |
| 只有 `started`、沒有結束列 | 嘗試中途中斷（bfx-deploy 被殺、主機重開） | 看 `docker ps` 與 journal 判斷停在哪；之後 `--retry` |

**migration 之前**失敗：沒有 migration 的部署，現行 release 不受影響，自動回滾只發生在這種情況。
有 migration 的部署在備份前就停了 bot，失敗時維持停止（Will 2026-09-25）。

**migration 之後**失敗：舊 code 可能不能在新 schema 上跑（live daemon 開機會比對
`core/schema_head.py` 的 `SCHEMA_HEAD` 與 `alembic_version`，不一致就寫 `HALTED/auto`、盡力
cancel-all、拒絕開機）。只能：

1. roll forward：修正後出新 release（新 commit → CI → GHCR → bfx-deploy）；或
2. 把 migration 前的備份還原到**隔離**資料庫比對，再決定。**不要把舊備份蓋回 production**
   ——venue 可能已有寫入，見 [rollback after a venue write](rollback-after-venue-write.md)。

bot 停下時 venue 上的掛單不會自己消失：停 bot 之前，若有掛單而且停機會超過幾分鐘，先在 UI 或
`/admin/halt` 執行 kill（見 operations）。bot 停著時兩者都不可用，改在 Bitfinex 網頁手動撤單並記錄。

## 6. Rollback drill（刻意走一次回滾路徑）

需要：已有至少一次成功部署、且這次 release 沒有待套用 migration（否則會被拒絕）。

```bash
sudo systemctl stop bfx-deploy.timer
sudo bfx-deploy --rollback-drill   # 部署新 release，健康後故意走回滾 → ledger rolled_back
sudo bfx-deploy --retry            # 真的部署同一個 digest
sudo systemctl start bfx-deploy.timer
```

驗收：ledger 依序有這次嘗試的 `started`、`rolled_back`（detail
`unhealthy:rollback_drill; rolled_back_to=<上一版>`），再一組 `started`、`deployed`；
Telegram 收到 warning 與 info；兩次之間 `docker inspect bfx-bot` 顯示的是上一版 digest。

## 7. 暫停自動部署

```bash
sudo systemctl stop bfx-deploy.timer      # 暫停；手動 bfx-deploy 仍可用
sudo systemctl start bfx-deploy.timer     # 恢復
```

`bfx-deploy.service` 的 `TimeoutStartSec=6h`（備份＋restore test＋pull＋migration 的最壞情況），
systemd 不會在 migration 中途把它砍掉。不要手動 kill 正在跑 migration 的 bfx-deploy。
