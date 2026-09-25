# Cutover runbook：immutable release → CI/GHCR/bfx-deploy（一次性）

把 production 從舊的 immutable-release 手動流程換到 [deploy runbook](deploy.md) 的
`bfx-deploy`。依據：`docs/superpowers/plans/2026-09-25-release-governance-refactor.md` §3–§4。
Production 在整個切換期間維持停機（halt 11），直到最後一步由 Will 以 TOTP 恢復。

完成後這份文件只剩歷史價值；日常操作看 [deploy](deploy.md) 與 [operations](operations.md)。

## 0. Will 的前置作業（secrets 只存 VM，回覆 `done`，不要貼出來）

1. **Bitfinex API key**：確認沒有提領權限、設定只允許 VM 對外 IP 的 IP whitelist、
   確認帳戶層級的 funding auto-renew 是關閉的。
2. **Telegram**：用 BotFather 建一個 bot，取得 token 與要收訊息的 chat id。寫進 **兩個**檔：
   - `/opt/bfx/runtime/notify.env`（新檔）：`TELEGRAM_BOT_TOKEN=...`、`TELEGRAM_CHAT_ID=...`
   - `/opt/bfx/runtime/bot.env`（加兩行，同樣的值）
3. **GHCR**：一個只有 `read:packages` 的 token，寫進 `/opt/bfx/runtime/ghcr.env`：
   `GHCR_USERNAME=...`、`GHCR_TOKEN=...`。

所有 `/opt/bfx/runtime/*.env`：`sudo chown root:root` 且 `sudo chmod 0600`。值不可含 `$`、
不可加引號、不可有 ` #`（見 [deploy §3](deploy.md#env-檔規則)）。

## 1. 合併與 image

1. review 後把 `refactor/release-governance` 合併進 `main` 並 push。
2. 等 GitHub Actions：`CI` 綠燈 → `Release images` 綠燈；job summary 列出 backend/frontend digest。
   記下 merge commit 的 40 位 revision。

## 2. VM 準備（不動正在跑的 container）

以下在 VM 上執行。

1. **主機工具前置檢查**：bfx-deploy 需要 `uv`（裝在 root 擁有的 `/usr/local/bin/uv`）與
   `docker buildx`（registry 查詢用 `imagetools inspect`），PyYAML／pathspec 由 uv 依
   `deploy/vm/ops/uv.lock` 裝進工具自己的環境，不裝進系統 Python：

   ```bash
   /usr/local/bin/uv --version        # 沒有就由 Will 決定安裝方式（對外下載，需同意）
   docker buildx version
   /usr/bin/python3 --version         # 3.12 以上
   ```

   任何一項缺少就停下回報；`install.sh` 也會先檢查前兩項。
2. **更新 VM checkout**（切換前的 pgBackRest timer 仍直接跑這個 checkout 裡的腳本，pull 前先看 diff）：

   ```bash
   cd /home/ubuntu/bfx-funding-bot
   git fetch origin
   git diff HEAD origin/main --stat -- deploy/vm/pgbackrest deploy/vm/systemd docker-compose.bot.yml
   git pull --ff-only
   ```

3. **第一次安裝工具與 unit**（之後由 bfx-deploy 自己更新；不會啟用任何 timer）：

   ```bash
   sudo deploy/vm/ops/install.sh
   ls -l /usr/local/lib/bfx-ops/current /home/ubuntu/bfx-releases/current
   ```

   它要求 checkout 沒有本地修改，從 git 物件寫出 `/usr/local/lib/bfx-ops/releases/<rev>`、
   跑 `uv sync --frozen`、建 wrapper 與 DR checkout（`/home/ubuntu/bfx-releases/<rev>`，
   `current` 指向它），並安裝 `deploy/vm/systemd/managed-units` 列出的 unit。之後
   pgBackRest 的 backup／status unit 改跑 `bfx-releases/current` 的腳本，不再跑這個 checkout。

4. **補齊 runtime env**。舊 launcher 會從 release bundle 注入帳戶與 operator 設定；新流程只讀
   `/opt/bfx/runtime/*.env` 與 repo 的 `deploy/vm/live.env`。只比對 key 名稱（不印值）：

   ```bash
   for c in bfx-bot bfx-webapi bfx-frontend; do
     echo "== $c"; sudo docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}' \
       | cut -d= -f1 | sort -u
   done
   sudo cut -d= -f1 /opt/bfx/runtime/bot.env | sort -u
   cut -d= -f1 deploy/vm/live.env | grep -v '^#' | sort -u
   ```

   舊 `bfx-bot` 有、而 `bot.env`＋`live.env` 都沒有的 key 要補進 `bot.env`（至少
   `BFX_EXCHANGE_ACCOUNT_ID=<canonical UUID>`；`BFX_OPERATOR_USER_ID` 與 `webapi.env` 相同）。
   `BFX_RELEASE_*` 是舊 launcher 專用，不要補。webapi／frontend 同樣比對。
   `migrate.env` 是 owner role 的 `DATABASE_URL`（bfx-deploy 的 migration 與下面的 policy 修改用）。

5. **restore test 設定** `/home/ubuntu/bfx/restore-test.json`（`ubuntu` 擁有）：

   ```json
   {"account_id": "<canonical UUID>", "environment": "prod", "projector_version": "execution-state-v1"}
   ```

6. **DR verifier image 檢查**：restore test 在 `bfx-bot:local` 裡跑 `prefix_verify.py`。第一次
   部署前 `bfx-bot:local` 還是舊 image，確認它有需要的模組：

   ```bash
   sudo docker run --rm --entrypoint "" bfx-bot:local /app/.venv/bin/python -c \
     "from bfx_funding_bot.modules.execution.event_store.canonical import rolling_prefix_hash; import scripts.verify_projection_replay; print('ok')"
   ```

   失敗就停下回報（不要自己重 tag），否則第一次部署會因 restore test 失敗而中止。

7. **先手動跑一次 restore test**（最多約一小時；第一次部署前在停機窗口外先驗證設定）：

   ```bash
   sudo systemctl start --no-block bfx-restore-test@current.service
   journalctl -fu bfx-restore-test@current.service
   ls -l /home/ubuntu/bfx/dr-evidence/restore-heartbeat.json
   ```

8. **告警測試**：`sudo bfx-notify --level info "bfx cutover: notify test"`，確認 Telegram 收到。

9. **Dry run**：

   ```bash
   sudo bfx-deploy --dry-run
   ```

   預期：`revision` 是 merge commit；`change_class` material（`no_previous_deployment`）；
   `migrations_pending` true、`stop_bot_before_migration` true；`restore_test_before_deploy` 有值；
   `ci_run` 是通過的 CI run URL；`rollback_target` null；
   `blockers` 只有三個 `foreign_container_holds_name:bfx-*`（舊 container 還在，下一步處理）。
   其他 blocker 或 precheck 失敗 → 修好再跑。

## 3. 切換窗口

1. **確認停機**：舊 bot 仍是 halt 11（`GET /admin/trading-status` 的 halt 為 halted），Bitfinex
   網頁上沒有 open funding offer。
2. **記錄舊 container 的 image 與 restart policy**（回滾與事後清理用）：

   ```bash
   sudo docker inspect --format '{{.Name}} {{.Image}} {{.HostConfig.RestartPolicy.Name}}' \
     bfx-bot bfx-webapi bfx-frontend
   ```

3. **停下並改名舊 container**（保留作為回復副本，不刪）：

   ```bash
   sudo docker stop --time 30 bfx-bot bfx-webapi bfx-frontend
   sudo docker update --restart=no bfx-bot bfx-webapi bfx-frontend
   for c in bfx-bot bfx-webapi bfx-frontend; do sudo docker rename "$c" "$c-pre-cutover"; done
   ```

   從這裡開始 UI 無法使用，直到 bfx-deploy 完成。

4. **手動部署一次**（用 systemd 跑，SSH 斷線不會中斷；最長 6 小時 timeout）：

   ```bash
   sudo systemctl start --no-block bfx-deploy.service
   journalctl -fu bfx-deploy.service
   ```

   順序：pull by digest → env 檢查 → 停 bot（舊 bot 已改名，會記 `bot absent`）→
   目標版本 DR checkout 的 `backup.sh --type diff` → `bfx-restore-test@<rev>` →
   `alembic upgrade head`（trading state、operator requests、release 表 archive＋drop、
   deployments ledger、pre-trade 限額、webapi read grants）→ ledger `started` 列 →
   `docker compose -p bfx-app up` → digest／身分檢查 → 健康＋60 秒 settle → 安裝這個 release 的工具。

   預期 ledger 同一個 `attempt_id` 兩列：`started` 與 `deployed`，`change_class=material`、
   `migrations_applied=true`，Telegram 收到 info。

5. **設定 fUST 單筆上限**（沒有它，`max_offer_amount` guard 會擋掉 fUST 的每一筆單）。
   用剛部署的 backend image 跑 one-shot（`migrate.env`＝owner role）：

   ```bash
   BD=<ledger 的 backend_digest>
   oneshot() {
     sudo docker run --rm --pull=never --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
       --user 1000:1000 --cap-drop=ALL --security-opt=no-new-privileges --network bfx_default \
       --workdir /app --entrypoint "" --env-file /opt/bfx/runtime/migrate.env \
       --env PYTHONDONTWRITEBYTECODE=1 \
       "ghcr.io/will413028/bfx-funding-bot-backend@$BD" /app/.venv/bin/python "$@"
   }
   oneshot -m scripts.amend_capital_policy --exchange-account-id <UUID> \
     --environment prod --symbol fUST --max-offer-amount 200
   # 檢查完整報告，記下它給的 digest，然後：
   oneshot -m scripts.amend_capital_policy --exchange-account-id <UUID> \
     --environment prod --symbol fUST --max-offer-amount 200 --apply-digest <DIGEST>
   ```

   `status` 必須是 `applied`；exit 2（`blocked`）就停下看 reason。這個指令不會 resume。

## 4. 驗證清單

- [ ] `docker ps`：`bfx-bot`、`bfx-webapi`、`bfx-frontend` 屬於 compose project `bfx-app`，
      image 是 GHCR digest；`bfx-postgres`、`bfx-redis` 未被重建。
- [ ] `deployments` ledger 有同一個 `attempt_id` 的 `started` 與 `deployed` 兩列，`ci_run` 是 CI run
      URL（[deploy §3](deploy.md#3-常用指令vmroot) 的查詢）；`docker inspect bfx-bot` 的
      `BFX_DEPLOYMENT_ID` 等於該 `attempt_id`。
- [ ] 工具已換成這個 release：`readlink /usr/local/lib/bfx-ops/current` 與
      `readlink /home/ubuntu/bfx-releases/current` 都是 merge commit。
- [ ] schema：`oneshot` 同樣方式跑 `/app/.venv/bin/alembic current` → `6f2b8d0e4a17 (head)`
      （把上面函式的 `python` 換成 `alembic`，或直接看 bfx-deploy 的 journal）。
- [ ] UI：fresh sign-in＋TOTP；交易狀態面板顯示「停機 · operator」（halt 11 已轉成
      `HALTED/operator`），後端 image＝ledger digest，變更分級 material，出現「核准這個 build」。
- [ ] uncertainty／history 頁面可讀（webapi read grants 由 migration `6f2b8d0e4a17` 提供，
      取代舊 runbook 的手動 GRANT）。
- [ ] **Kill switch**：UI 輸入 `KILL`（reason「cutover verification」）→ 面板的 venue 撤單
      每個幣別 `acknowledged`（預期原本就沒有掛單）；Telegram 收到交易狀態告警。
- [ ] 緊急備用路徑：只確認 `GET /admin/trading-status` 可用 static token 讀到狀態。不要在
      HALTED 時測 `/admin/pause`（HALTED→REDUCING 是非法轉換）。
- [ ] fUST `max_offer_amount` 已套用：再跑一次不帶 `--apply-digest` 的 amend，報告顯示目前值 200。
- [ ] 啟用 timer：

      ```bash
      sudo systemctl enable --now bfx-deploy.timer bfx-backup-check.timer bfx-restore-test.timer
      systemctl list-timers 'bfx-*'
      ```

      下一次 bfx-deploy 應記錄 `up to date`；bfx-backup-check 不告警
      （restore heartbeat 由第 2 節與部署前的 restore test 寫入）。

## 5. 恢復交易（Will）

1. UI「核准這個 build」（TOTP）→ 結果「approved; trading state HALTED unchanged」。
2. UI「恢復交易」（TOTP）→ `ACTIVE` 限額期：新單上限為正常 cell 的 25%（至少一筆 venue 最小單），
   24 小時且 ≥3 筆 acknowledged submit、期間沒有 HALTED 時自動解除。Telegram 收到
   `probation_started`，解除時收到 `probation_lifted`。
3. 限額期中觀察：面板進度、`docker logs bfx-bot`、Grafana。有任何自動保護 → 依
   [operations §4](operations.md#4-自動保護寫-haltedauto--kill-path不會自動解除) 處理。

## 6. 回滾邊界

| 失敗發生在 | 做法 |
|---|---|
| migration 之前（precheck、備份、restore test、`migration_failed`；schema 未變） | 回到舊 container：`docker stop` 任何 `bfx-app` container（若有）、`docker rm` 它們以釋放名稱，把 `*-pre-cutover` 改回原名、以 `docker update --restart=<第 3 節記下的 policy>` 還原、`docker start`。舊 code 仍對應舊 schema。修好後重新從第 3 節開始 |
| migration 之後（`migration_partial_or_unverified`，或 `unhealthy ...; migrations applied`） | **不要**啟動舊 container（舊 code 不能跑新 schema）。roll forward：修正後出新 release；或把 migration 前的備份還原到**隔離**資料庫比對再決定。不要把備份蓋回 production（[rollback after a venue write](rollback-after-venue-write.md)）|

第一次部署沒有「上一個成功部署」，所以不健康時 bfx-deploy 只會停 bot，不會自動回滾。

## 7. 之後

- 下一個沒有 migration 的 release（例如只改文件的 standard release）部署前，做一次
  [rollback drill](deploy.md#6-rollback-drill刻意走一次回滾路徑)（T10 驗收：刻意回滾一次）。
- 限額期通過、rollback drill 完成、穩定運作一段時間後，由 Will 決定清理下列 VM 上的舊東西。
  **這些都是 repo 外、未追蹤的檔案或 container，刪除前每一項都要 Will 同意；本文件不執行任何刪除。**

  | 路徑／物件 | 是什麼 | 注意 |
  |---|---|---|
  | `bfx-bot-pre-cutover`、`bfx-webapi-pre-cutover`、`bfx-frontend-pre-cutover` | 舊 container（回復副本） | 確定不再需要回到舊流程後 `docker rm` |
  | 第 3 節記下的舊 image ID | 舊 launcher load 的 image | container 刪掉後才能 `docker image rm`；`bfx-bot:local` 已重新指向新 image，不要刪這個 tag |
  | `/opt/bfx/tooling/` | 舊 immutable-release host CLI 的 checkout 與 uv/venv | — |
  | `/opt/bfx/releases/` | 舊 release bundle、manifest、`backend.tar`／`frontend.tar` | — |
  | `/opt/bfx/receipts/` | 舊 schema/policy/launch/DR/operator 收據 | 先打包留存（真錢期的 release/permit 資料已在 DB 的 `release_archive`，檔案收據沒有） |
  | `/home/ubuntu/bfx/retired/backend_py-leftover-20260924` | 目錄改名留下的快取 | — |
  | `/home/ubuntu/bfx/{bot,webapi,frontend}.env`、`.bak` | `deploy-vm.sh` 時代的 env 來源 | 含 secrets；先確認 `/opt/bfx/runtime/` 已涵蓋所有 key。repo 根目錄的 `.env.runtime` 仍被 `bfx-postgres` 使用，**不可刪** |

  不要刪：`/opt/bfx/runtime/`、`/var/lib/bfx-deploy/`、`/usr/local/lib/bfx-ops/`、
  `/home/ubuntu/bfx-releases/`（bfx-deploy 自己修剪）、`/home/ubuntu/bfx/dr-evidence/`、
  `/home/ubuntu/bfx/restore-test.json`、pgBackRest 設定與 secrets。
