# Deploy runbook（CI → GHCR → bfx-deploy）

現行唯一的應用程式部署路徑。決策來源：ADR 2026-09-25
`ci-registry-digest-deploy`（供應鏈）與 `lending-envelope-replaces-probation-and-account-halt`
（D5：部署不碰交易狀態、沒有分級與核准）。交易狀態與停機操作見 [operations runbook](operations.md)，
首次安裝主機工具見 [fresh host setup](fresh-host-setup.md#3-首次安裝主機工具)。

舊的 immutable-release 流程（bundle、release session、canary permit、halt epoch、Halt 2
收據、`/opt/bfx/releases` 手動 load）與 `scripts/deploy-vm.sh` 已刪除，不要再照舊文件操作。

## 1. 流程總覽

```
push main ──► CI (.github/workflows/ci.yml) 綠燈
              含 compose hardening policy（docker compose config → compose_policy.py）
          ──► Release images (.github/workflows/release.yml)
                arm64 backend image（含營運 UI 的 static export）→ GHCR
                tags: sha-<40 位 revision>；main 只在該 revision 仍是 main 的 head 時才推
                （workflow_run 可能亂序完成，舊 revision 不得把 main tag 拉回去）
                labels: org.opencontainers.image.revision、bfx.ci-run-url（通過的 CI run）
                provenance：actions/attest 對推上去的 digest 簽 SLSA build provenance
                （Sigstore public-good keyless），attestation 存在 repo 的 GitHub attestation API
          ──► VM bfx-deploy.timer（每 5 分鐘，*:2/5）→ bfx-deploy
```

- Image：`ghcr.io/will413028/bfx-funding-bot-backend`（9d-5 起不再建 `…-frontend`；9d-6 起 bfx-deploy 也不查它，
  compose 檔帶 frontend 服務的 revision 會在 precheck 被拒）。
  `main` tag 只用來「發現」最新 release；部署一律以 `repository@sha256:<digest>`。
- Registry 查詢用 `docker buildx imagetools inspect`（GHCR 憑證只放在每次呼叫的暫時
  `DOCKER_CONFIG`，不留下 `docker login`）。
- 營運 UI 唯一的 build 參數是 GitHub repository variable `NEXT_PUBLIC_SENTRY_DSN`（公開設定、非 secret；
  未設定時 release 直接失敗；backend/Dockerfile 的 `ui` stage 從 named context `frontend` build）。改它就是改
  image：之後的下一個 `main` commit 才會帶進新 image。入口見 [fresh host setup](fresh-host-setup.md#2-tailnet-入口營運-ui)。
- VM 從不 build image。

### bfx-deploy 一次執行做什麼

1. `flock`（`/run/lock/bfx-deploy.lock`），兩個執行不會重疊。
2. 解析 GHCR `backend:main` 的 digest；若 ledger 最新的成功部署已是它就結束。
   **上次失敗、rolled_back 或中斷（只有 `started` 列）的 digest 不會自動重試**，要 `--retry`。
3. 讀 image label 的 revision 與 CI run，要求 `backend:sha-<rev>` 是同一個 digest；
   接著驗這個 digest 的 build provenance，**不用任何 GitHub token，GHCR package 可以維持 private**：
   1. 用 bfx-deploy 自己的 registry 存取（`ghcr.env` 的 pull 憑證，和 pull 用的是同一份）以
      `docker buildx imagetools inspect <repo>@<digest> --raw` 讀出 manifest（多平台 push 時是 index）
      的原始 bytes，要求 sha256 等於該 digest，寫成暫存檔；
   2. 匿名讀 `https://api.github.com/repos/Will413028/bfx-funding-bot/attestations/<digest>`，逐一下載每筆的
      `bundle_url`（只接受 https、上限 2 MiB；內容是 snappy 壓縮，bfx-deploy 用內建的解碼器解開）；
   3. 用釘版的 gh（`/usr/local/lib/bfx-ops/gh/2.102.0/bin/gh`）逐一驗，直到有一筆通過：
      `gh attestation verify <manifest 檔> --bundle <bundle 檔> --repo Will413028/bfx-funding-bot
      --signer-workflow Will413028/bfx-funding-bot/.github/workflows/release.yml --source-ref refs/heads/main
      --deny-self-hosted-runners --format json`。同一個 digest 可能有別種 attestation（例如 SBOM），
      gh 會拒絕那些，所以一筆筆試；全部被拒才算驗不過。

   gh 的環境從零建立：沒有 token、沒有 gh login、沒有 docker 設定，`ghcr.env` 的 pull 憑證只給 registry 那一步，
   不交給 gh，也不送到 GitHub API。`gh attestation verify --bundle` 不需要登入（實測 2026-10-22：`env -i` 下 rc 0）；
   gh 只連 Sigstore 的 TUF CDN 取信任根，cache 在 `/var/lib/bfx-deploy/gh/`。匿名 GitHub API 每個 IP 每小時
   60 次；bfx-deploy 只在有未部署的新 release 時每輪打 1 次（最多每小時 12 次）。
   必須在第 5 步之前：那一步會在新 image 裡以 DB owner（`migrate.env`）跑 alembic。驗不過就跟「release 還找不到」
   一樣處理：不寫 ledger、什麼都不動、每一輪重驗，連續失敗 30 分鐘發一次 warning（見 §5「Provenance 驗不過」）。
   沒有略過驗證的旗標。同一套檢查可以手動跑：`sudo bfx-deploy verify-provenance <digest>`。
4. 在 VM mirror（`/home/ubuntu/bfx-funding-bot`，以 `ubuntu` 身分跑 git）fetch，要求 `<rev>`
   在 `origin/main` 上，而且 ledger 最新成功部署的 revision 等於 `<rev>` 或是它的祖先（不降版：
   `main` tag 被指回一個較舊、簽章同樣有效的 digest 時 precheck `release_not_descendant_of_deployed`）；compose 檔與 `live.env` 都取自 `<rev>` 本身（git 物件，不讀 working tree），
   寫進 `/var/lib/bfx-deploy/releases/<rev>/`。**要跑什麼由這份 compose 檔決定**：
   `docker compose config --no-env-resolution --format json`（不讀 env 檔內容）渲染出服務、
   `container_name`、image 與 env 檔，部署、檢查、健康等待都只針對這些服務。ledger 的 `frontend_digest`
   只是 9d-5 之前的歷史，新列一律記 NULL、部署不讀它。
   頂層 `configs` 的 `file`（只接受 release 目錄內的檔案；`content`／`environment` 形式一律拒絕；頂層
   `secrets` 只接受 `<runtime dir>/secrets/<service>/<KEY>` 的檔案，見 [秘密檔](#秘密檔botwebapi9h)）同樣從 `<rev>` 的 git 物件寫到 release 目錄的同一個相對路徑；
   compose 沒有 `configs` 就什麼都不多寫，所以回滾或 `--recreate` 到沒有這些檔的舊 revision 照常運作。
   引用了但 `<rev>` 沒有這個檔：`release_file_missing`，什麼都還沒停。
5. 以 digest pull compose 裡的每個 image：release 自己的 backend 帶 GHCR 憑證，
   第三方 image 只允許 `docker.io/tailscale/tailscale@sha256:…`（`THIRD_PARTY_IMAGE`）且不帶憑證；
   其他 image 一律拒絕。9d-5 之前、帶 frontend 服務的 revision 在 precheck 被拒、什麼都還沒停（它的 `${BFX_FRONTEND_IMAGE:?}` 已沒人提供，渲染即失敗；寫死 digest 的則是 `compose_image_not_allowed:frontend`）。檢查 compose 引用的秘密檔與 env 檔、`migrate.env` 與 `live.env`；在新 image 的 one-shot
   （`migrate.env`，owner role）比較 `alembic current` 與 `alembic heads`。
6. **有待套用的 migration**（Will 2026-09-25；全停：ADR
   `2026-10-15-tailscale-native-operator-ingress-and-cli-only-high-risk-operations` D6）：
   先停 webapi，再停 bot（唯一的 writer）→
   用 `<rev>` 的 `backup.sh --type diff` 備份 → 用 `<rev>` 的 DR 腳本跑 isolated restore test
   （`bfx-restore-test@<rev>.service`；驗證還原之後，用 `<rev>` 的 backend image 在隔離副本上
   演練 `alembic upgrade head` 與 boot check，[offsite-dr](offsite-dr.md)）→ `alembic upgrade head`。
   webapi 停不了：什麼都不做，bot 照跑；bot 停不了：什麼都不做，把剛停的 webapi 重新啟動。
   **兩者都停下後任何一步失敗，bot 與 webapi 都維持停止**（舊 webapi 不在新 schema 上跑），
   只能 roll forward（新 release）或 `--retry`；
   唯一例外是演練失敗（receipt `error_code: migration_rehearsal_failed`）：備份已證明可還原、
   prod schema 沒動，bfx-deploy 依序把停下的 bot 與 webapi 重新啟動，各自等健康與 settle
   （D7'，Will 2026-10-08）。
   **這段期間（停 webapi 到新版健康）營運 UI 不可用**（UI 由 webapi 提供）
   （停機前撤單見下方「migration 之後」末段）。
   **沒有 migration 但 diff 碰到 DR 路徑**（`deploy/vm/pgbackrest/**`、`deploy/vm/postgres/**`、
   `docker-compose.bot.yml`、`docker-compose.dr.yml`，或 diff 讀不到）：什麼都不停，先跑 restore test。
   沒有 migration 的部署與回滾路徑不停 webapi。
7. 寫入 ledger 的 `started` 列（attempt id、digest、revision、CI run；之後不可修改），
   再 `docker compose -p bfx-app -f <release>/docker-compose.app.yml up -d --no-deps --remove-orphans <compose 的服務>`；
   compose 不再有的服務以 `--remove-orphans` 移除。
   自家服務每次都會重建（身分 env 每次不同）；沒變的第三方容器（holder、tailscale）Compose 會沿用，所以平常的
   部署不會重啟 tailnet 節點。只有兩種情況加 `--force-recreate` 重建全部：它掛載的設定檔（serve.json）和這次
   release 的內容不同（Compose 自己不會因此重建），或加入者卡在已死的 namespace（holder 被單獨重啟或停掉過）。
   `BFX_DEPLOYMENT_ID` 只用來讓 audit 與狀態報告指出這次部署，不影響交易。
8. 檢查每個 container 跑的是 compose 渲染出的 image、屬於 compose project `bfx-app`；release 自己的
   image 另外比對身分 env（`BFX_IMAGE_DIGEST`、`BFX_SOURCE_REVISION`、`BFX_DEPLOYMENT_ID`）；
   加入另一個服務 network namespace 的服務（`network_mode: service:<X>`，9d-5 的 webapi）要在 `<X>`
   目前的 namespace 裡（`HostConfig.NetworkMode` 等於 `container:<X 的 Id>`，且晚於 `<X>` 啟動），否則
   `container_release_mismatch:<服務>:netns`。兩者一定在同一次 `up`：`<X>` 被重建時 Compose 會連帶重建加入它的
   服務（Compose v5.1.4 實測），這條檢查證明它真的發生。
   等健康（bot `:8080/healthz`、webapi `:8000/ready`（會連 DB；`/health` 在死掉的 netns 裡仍回 ok）；其他服務一定要在 compose 宣告 `healthcheck`，看 Docker 的 `State.Health.Status` 是
   `healthy`，沒宣告就 precheck `compose_healthcheck_missing:<服務>`），再 settle 60 秒。
   webapi 與 bot 一樣，開機時比對 image 內 migrations 的唯一 head（`core/schema_head.py`）與
   `alembic_version`，不一致就拒絕開機（uvicorn 結束、health 不會通過）：舊 webapi 不會在
   migrate 過的 schema 上服務，新 webapi 也不會在還沒 migrate 的 schema 上服務。
   Hardening（read-only、UID、cap-drop、port、network）不在 VM 上重驗，而是 CI 對
   `docker compose config` 的輸出做 policy 檢查（`deploy/vm/ops/compose_policy.py`）。
9. 成功：`bfx-bot:local` 重新 tag 到新 backend（DR verifier 仍用這個 alias；weekly report 改用 ledger 的 digest）；
    安裝 `<rev>` 的主機工具與 systemd unit（見 §4，**下一輪才生效**）；DR checkout 的 `current`
    指到 `<rev>`；刪掉目前與上一版以外的舊 digest、工具版本與 DR checkout。
10. 寫入 ledger 的結束列（`deployed`／`rolled_back`／`failed`，與 `started` 列同一個 attempt id），
    並發 Telegram（deployed＝info、有 warning 或 rolled_back＝warning、其他＝critical）。
    在變更任何 container 之前就失敗的嘗試只有結束列。

PostgreSQL 在 `docker-compose.bot.yml`（project `bfx`），bfx-deploy 從不碰；
app 只加入既有的 `bfx_default` network。這個檔只定義 `postgres`
（`test_offsite_dr_image.py` 守住）；app 的舊 compose 定義已刪除。Redis 隨 app 登入移除（9d-6）：
容器以 `docker rm -f bfx-redis` 移除（compose 檔已沒有這個服務，`compose rm redis` 找不到它），
volume（`docker volume ls --filter name=redisdata`）保留 30 天後再刪。

### 套用資料服務的 compose 變更

`bfx` project 的工作目錄是 VM 上的 `/home/ubuntu/bfx-funding-bot` checkout（postgres 讀的 `.env.postgres.runtime` 也在這裡：只有 `POSTGRES_USER`／`POSTGRES_PASSWORD`／`POSTGRES_DB`，0600），
不是 release checkout；只改 `postgres` 會讓 DB 重啟，所以要在約好的維護窗口做，且先停 bot。
`bfx-postgres:local` 會被 DR 流程從 release 重建，所以 `up` 一定會 recreate postgres，
即使 compose 沒變；只能用 `--no-deps --no-build` 指名服務。

```bash
cd /home/ubuntu/bfx-funding-bot
git pull --ff-only origin main             # 到已部署的 revision
cmp docker-compose.bot.yml /home/ubuntu/bfx-releases/current/docker-compose.bot.yml
docker compose -p bfx -f docker-compose.bot.yml up -d --no-deps --no-build --dry-run postgres
sudo systemctl stop bfx-deploy.timer       # 窗口內不讓新 release 把 bot 拉起來
docker stop bfx-bot
docker compose -p bfx -f docker-compose.bot.yml up -d --no-deps --no-build postgres
docker inspect -f '{{.State.Health.Status}} init={{.HostConfig.Init}}' bfx-postgres   # healthy init=true
docker exec bfx-postgres ps -o pid,comm | sed -n 2p                                   # 1 docker-init
docker exec --user postgres bfx-postgres pgbackrest --stanza=bfx check                # 推得出 WAL
docker exec --user postgres bfx-postgres psql -d bfx -Atc "CREATE EXTENSION IF NOT EXISTS pg_stat_statements; SELECT count(*) >= 0 FROM pg_stat_statements"   # t（慢查詢，operations §8）
docker start bfx-bot && sudo systemctl start bfx-deploy.timer
```

dry run 只能列出 `bfx-postgres Recreate`；出現其他服務或 orphan 容器就停下來查。`pgbackrest check`
失敗時 bot 維持停止，先修 archive（RPO 由 `bfx-backup-check` 監看）。

## 2. 部署與交易狀態

部署永遠不改變 trading state，也不需要任何核准：每筆單都由 DB 裡的 CapitalPolicy 包絡把關
（見 [operations §2](operations.md#2-幣別啟停與包絡設定)）。變更分級
（standard／material）已退役；compose 不再注入 `BFX_CHANGE_CLASS`，migration
`5b9e3d7a2f41` 移除 `deployments.change_class`。歷史分級保存在舊 ledger 列的 `detail`。

## 3. 常用指令（VM，root）

```bash
sudo bfx-deploy --dry-run            # 發現、fetch、pull、比 schema；不改任何東西，輸出 JSON plan
sudo bfx-deploy                      # 手動跑一次（timer 平常自己跑）
sudo bfx-deploy --retry              # 重試上次 failed/rolled_back/中斷的同一個 digest
sudo bfx-deploy --recreate           # 同一個 release 重建 container（改了 runtime env 之後）
sudo bfx-deploy --recreate --dry-run # 預覽 recreate
systemctl list-timers 'bfx-*'
journalctl -u bfx-deploy.service -n 200 --no-pager
```

`--dry-run` 的 JSON 看 `ci_run`、`migrations_pending`、
`stop_bot_before_migration`、`stop_webapi_before_migration`、`restore_test_before_deploy`、
`rollback_target`、`blockers`
（例如 `foreign_container_holds_name:bfx-bot`）。

部署紀錄（最新在上；同一次嘗試的 `started` 與結束列共用 `attempt_id`）：

```bash
docker exec --user postgres bfx-postgres psql -U bfx -d bfx -c \
  "SELECT id, attempt_id, outcome, left(source_revision,12) rev, migrations_applied,
          finished_at, service_degraded, detail, ci_run
     FROM deployments ORDER BY id DESC LIMIT 10"
```

`service_degraded`（結束列）：這次嘗試結束時 bot 或 webapi 是否停著或不健康。`failed` 而 `false` 的是沒動到
正式環境（precheck、停機前的 restore test）或已重新啟動回原 release；`started` 列與欄位出現前的列是 NULL。
每週報告的交付指標（`scripts/report_delivery_metrics.py`）用它算 change failure rate 與恢復時間。

目前在跑的 digest 也可用 `docker inspect bfx-bot --format '{{.Config.Image}}'` 或 UI 交易狀態面板的
「後端 image」確認。

### `--recreate`（改了 runtime env 之後）

只改 `/opt/bfx/runtime/*.env` 或 `/opt/bfx/runtime/secrets/` 不會觸發部署。`--recreate` 用 ledger 最新 `deployed` 列的
digest 與 revision，走同樣的 env 檢查、`started` 列、`--force-recreate`、身分檢查、健康等待與
結束列。
recreate 沒有 rollback（舊 env 已經不在了）：不健康就停 bot、記 `failed`，修好 env 再 `--recreate`。
schema 不在該 release 的 head 時拒絕。

### Env 檔規則

`/opt/bfx/runtime/{bot,webapi,tailscale,migrate,notify,ghcr}.env`：root 擁有、mode `0600`、
不可是 symlink（`ubuntu` 使用者的 checkout 由 DR unit 執行，secret 不能讓它讀到）。
bfx-deploy 啟動前會檢查，違反即 `precheck` 失敗：

- 每行 `KEY=VALUE`，不可重複 key；
- 交給 compose 的檔（bot/webapi/tailscale 與 `live.env`）的值不可含 `$`、不可有包住的引號、
  不可含 ` #`：這些 secret 當初是為 `docker --env-file`（字面值）寫的，compose 的 env_file
  會做內插、去引號、截註解，值會默默變成另一個 secret；
- 身分 env（`BFX_IMAGE_DIGEST` 等）由 compose 的 `environment:` 注入，優先於 env 檔，寫在 env 檔
  也不會生效；
- compose 檔的 env_file 寫成 `${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/<name>.env`：bfx-deploy 傳入
  自己檢查過的 `--runtime-dir`；CI 用放了空檔的暫存目錄 render。預設值必須維持 `/opt/bfx/runtime`
  （CI 在 `config --no-interpolate` 的輸出上由 `compose_policy.py` 檢查，也要求 `required: true`），env 檔也維持必填，缺秘密檔時 `up` 會失敗而不是無聲啟動；
- `ghcr.env`：`GHCR_USERNAME`、`GHCR_TOKEN`（read-only `read:packages`）；檔案不存在＝匿名 pull。
  9i 的 provenance 驗證也用它讀 manifest（只給 registry，不給 gh）；驗證本身不需要任何 GitHub token。
  要拿掉 `ghcr.env`（並撤銷 PAT）就得先把 GHCR package 改成 public，那是 9h 的選項，不是 9i 的前提。
- `bot.env`／`webapi.env` 從 9h 起**不放秘密**：秘密改成下一節的秘密檔。留下的是 `TELEGRAM_CHAT_ID`、
  `SENTRY_DSN`（依設計公開，見 [secret-rotation](secret-rotation.md#sentry-dsn)）、`BFX_EXCHANGE_ACCOUNT_ID`
  與 webapi 的入口設定這類識別值。
- `notify.env`：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`（見 [operations §6](operations.md#6-告警)）。
- `tailscale.env`：只有 `TS_AUTHKEY`（tagged `tag:bfx-ops`、非 ephemeral、單次使用、短效期）。只在節點第一次
  登入時用（`TS_AUTH_ONCE=true`），之後節點的 key 存在 volume `bfx_tsstate`；key 過期或用過都不影響已登入的節點。

### 秘密檔（bot、webapi；9h）

bot 與 webapi 的常駐秘密是 compose 的檔案型 secret，不放在 env：

| 主機檔案（`/opt/bfx/runtime/secrets/`） | 容器內 | 誰讀 |
|---|---|---|
| `bot/DATABASE_URL`（`bfx_bot`）、`bot/BFX_ADMIN_TOKEN`、`bot/BFX_ADMIN_READ_TOKEN`、`bot/BFX_VAULT_KEK`、`bot/TELEGRAM_BOT_TOKEN` | `/run/secrets/<KEY>` | bot |
| `webapi/DATABASE_URL`（`bfx_webapi`）、`webapi/BFX_ADMIN_READ_TOKEN`（與 bot 的同值） | `/run/secrets/<KEY>` | webapi |

- 每個檔只有值本身（不含 `KEY=`，結尾換行無所謂）。
- 擁有者與權限：`root:4600`、mode `0640`。目錄 `secrets/`、`secrets/bot`、`secrets/webapi` 是 `root:root 0700`。
  - 為什麼不是 root 0600：compose 的檔案型 secret 是把主機檔案直接 bind mount 進容器，`uid`／`gid`／`mode` 設定會被
    默默忽略（Docker 文件），bot 與 webapi 以 UID 1000 執行，讀不到 root 0600 的檔。
  - 所以兩個服務以 `group_add: ["4600"]` 加入 group 4600（主機上的名稱是 `bfx-secrets`，沒有任何成員），靠 group 讀權讀檔。
  - 主機上的 `ubuntu`（也是 UID 1000）不在這個 group，目錄又是 0700，讀不到。
- gid 固定是 4600：`compose_policy.SECRET_GID` 與 `bfx_deploy.SECRET_GID` 兩處寫死，測試要求兩者相等。
- **bfx-deploy 部署前檢查**每個 release 掛載的檔案。檢查項目：root 擁有、group 4600、mode 0640、一般檔案、不是
  symlink、不是空檔；上面兩層目錄（`secrets/`、`secrets/<service>/`）是真的目錄（不是 symlink）、root 擁有、
  group 與 other 都不能寫（能寫目錄的人就能在檢查之後換掉檔案）。
  - 違反時 `precheck` 失敗，錯誤碼是 `secret_file_<原因>:<service>/<KEY>` 或 `secret_dir_<原因>:<目錄>/`，跑中的版本不受影響。
  - compose 檔裡的 secret 只能是 `<runtime dir>/secrets/<service>/<KEY>`，而且只能由該 service 掛載
    （`compose_secret_*` 錯誤碼）。
  - 就算檢查沒擋到，Docker 也不會建立缺 secret 檔的容器。
- 容器以 `BFX_SECRETS_DIR=/run/secrets` 讀檔（`core/secret_files.py`），**完全不看 env 裡同名的 key**：
  - 缺檔或空檔就拒絕開機，exit 1，log 是 `config_fatal secret file ... is missing`。
  - env 檔裡還留著這些 key 時照樣開機，但 log 會印 `secret_env_keys_ignored keys=...`（只印名稱）。
- one-shot 容器（operator CLI、每週報告、研究、模擬 soak、migration、DR）照舊用各自的 env 檔，不受影響。
- 換值與寫入：照 [secret-rotation「通用寫法」](secret-rotation.md#通用寫法)。值不上任何 argv（`sudo` 會把整行指令記進
  auth.log），整步在 `sudo bash -euo pipefail <<'SH'` 的 root shell 裡做；先寫同目錄的暫存檔，非空才 `mv`。

#### 9h 切換（一次性，依序；在 merge 9h 的 PR 之前做第 1–3 步）

9h 分兩個 release 出貨，因為 bfx-deploy 跑的是**上一個成功 release** 的主機工具（§1 第 10 步）：
- 第一個 release（`feat(deploy): read resident secrets through one reader ...`）讓 bfx-deploy 接受並檢查秘密檔、
  讓 backend 會讀秘密檔，但 compose 還不用它們。沒有 `BFX_SECRETS_DIR` 的 bot 與 webapi 照舊從 env 讀，
  唯一的行為差異是：**webapi 的 env 裡有 `BFX_VAULT_KEK`（即使是空值）就拒絕開機**（`boot_refused`，部署失敗、自動回滾）。
  所以 merge 第一個 release 之前，先確認 webapi 沒有這個 key（只數名稱、不印值；兩行都必須印 0）：

  ```bash
  sudo grep -c '^BFX_VAULT_KEK=' /opt/bfx/runtime/webapi.env
  docker inspect bfx-webapi --format '{{range .Config.Env}}{{println .}}{{end}}' | cut -d= -f1 | grep -cx BFX_VAULT_KEK
  ```

- 第二個 release（`feat(deploy): mount the bot's and web api's secrets as files`）才把 compose 改成掛秘密檔。
  這一節說的「9h 的 PR」指第二個。

第二個 release 沒有 env fallback：VM 上沒有秘密檔，新版 bot／webapi 就開不了機（precheck 會先擋下，舊版照跑）。

0. 確認第一個 release 已部署成功，而且它的工具已經裝上（必須印 1；印 0 就是舊工具，它會以
   `compose_secrets_refused` 拒絕第二個 release）：

   ```bash
   sudo grep -c '^def shared_secret_file' /usr/local/lib/bfx-ops/current/ops/bfx_deploy.py
   ```

1. 建 group 與目錄（root）：

   ```bash
   getent group 4600                      # 必須沒有輸出；有輸出代表 4600 已被佔用：停下回報
   sudo groupadd --gid 4600 bfx-secrets
   sudo install -d -o root -g root -m 0700 /opt/bfx/runtime/secrets \
     /opt/bfx/runtime/secrets/bot /opt/bfx/runtime/secrets/webapi
   ```

2. 從現有的 env 檔寫出秘密檔，過程不印值：

   ```bash
   sudo bash -euo pipefail <<'SH'
   umask 077
   move() {  # <service> <KEY>：把 <service>.env 的 KEY 值寫成 secrets/<service>/<KEY>（先寫同目錄的暫存檔，非空才 mv）
     src=/opt/bfx/runtime/$1.env dst=/opt/bfx/runtime/secrets/$1/$2
     [ "$(grep -c "^$2=." "$src")" = 1 ] || { echo "STOP: $1.env has no single non-empty $2"; exit 1; }
     tmp=$(mktemp "$dst.XXXXXX")
     if ! grep "^$2=" "$src" | cut -d= -f2- | tr -d '\n' > "$tmp" || [ ! -s "$tmp" ]; then
       rm -f "$tmp"; echo "STOP: $1/$2 not written"; exit 1
     fi
     chown root:4600 "$tmp"; chmod 0640 "$tmp"; mv "$tmp" "$dst"
   }
   for k in DATABASE_URL BFX_ADMIN_TOKEN BFX_ADMIN_READ_TOKEN BFX_VAULT_KEK TELEGRAM_BOT_TOKEN; do move bot "$k"; done
   for k in DATABASE_URL BFX_ADMIN_READ_TOKEN; do move webapi "$k"; done
   SH
   ```

   印出 `STOP` 就停下，例如 `bot.env` 沒有 `TELEGRAM_BOT_TOKEN`：先補好 env 檔，或先決定不要這個 key。這個 release
   要求七個檔都在、都不是空的。值只在 root shell 的 pipe 與檔案之間流動，不上任何 argv（`grep` 的參數只有 key 名稱）。

3. 核對秘密檔（只比對、不印值），然後用跑中的 image 實際讀一次。這一步驗證 gid 機制：Docker Desktop 上量不到，
   只有在 VM 上才算數。`bfx-op halt` 走的 `docker exec bfx-bot` 這時還驗不到：第一個 release 的 bot 容器沒有
   `group_add`，也沒有掛秘密檔，所以那一項留到第 4 步、第二個 release 部署之後第一件事做：

   ```bash
   sudo stat -c '%U:%g %a %s %F %n' /opt/bfx/runtime/secrets/*/*
   # 每行都要是 root:4600 640 <大於 0> regular file
   sudo bash -c 'for f in /opt/bfx/runtime/secrets/*/*; do s=$(basename "$(dirname "$f")"); k=$(basename "$f");
     cmp -s <(grep "^$k=" /opt/bfx/runtime/$s.env | cut -d= -f2- | tr -d "\n") "$f" && echo "same $s/$k" || echo "DIFF $s/$k"; done'
   # 七行都要是 same
   IMG=$(docker inspect bfx-bot --format '{{.Config.Image}}')
   sudo docker run --rm --pull=never --read-only --user 1000:1000 --group-add 4600 --network none --entrypoint /app/.venv/bin/python \
     -v /opt/bfx/runtime/secrets/bot/BFX_VAULT_KEK:/run/secrets/BFX_VAULT_KEK:ro "$IMG" \
     -c 'print(len(open("/run/secrets/BFX_VAULT_KEK").read().strip()) > 0)'     # 必須印 True
   sudo docker run --rm --pull=never --read-only --user 1000:1000 --network none --entrypoint /app/.venv/bin/python \
     -v /opt/bfx/runtime/secrets/bot/BFX_VAULT_KEK:/run/secrets/BFX_VAULT_KEK:ro "$IMG" \
     -c 'open("/run/secrets/BFX_VAULT_KEK")' 2>&1 | grep -c PermissionError   # 沒有 group 時必須讀不到：印 1
   ```

4. merge PR。bfx-deploy 在 5 分鐘內部署；要立刻部署就 `sudo systemctl start bfx-deploy.service`。部署後核對：
   - ledger 最新列是 `deployed`（§3 的查詢）；
   - **在其他任何核對之前**，確認 `docker exec` 進跑中的 bot 讀得到秘密檔（`bfx-op halt` 就是這樣讀 token 的；
     exec 出來的行程要帶到容器的 group 4600 才讀得到）。只印長度，必須是大於 0 的數字：

     ```bash
     sudo docker exec bfx-bot /app/.venv/bin/python -c 'print(len(open("/run/secrets/BFX_ADMIN_TOKEN","rb").read()))'
     ```

     印出 `PermissionError` 或 0：**不要做第 5 步**。這時 bot 本身照跑，kill 的主路徑 `sudo bfx-op kill`（經資料庫，
     不用 admin token）照常可用，只有緊急備用的 `bfx-op halt` 讀不到 token。回到第一個 release 的 compose：在 `main`
     revert 第二個 release 的 merge commit（PR → CI → bfx-deploy 部署），env 檔裡的舊 key 還在（第 5 步還沒做），
     所以它照常開機；再查 exec 為什麼沒帶到 group 4600。
   - `docker logs --since 15m bfx-bot 2>&1 | grep -c secret_env_keys_ignored` 印 1：env 檔裡還有舊 key，這時是預期的；
   - **緊急停止（kill path）**：`bfx-op halt` 在 bot 容器裡改讀 `/run/secrets/BFX_ADMIN_TOKEN`。
     不真的 halt，也能確認 bot 收這個檔的 token：用同一個檔打唯讀的 `/admin/trading-status`，必須印 200：

     ```bash
     sudo docker exec bfx-bot /app/.venv/bin/python -c 'import os,urllib.request as u
     t=open(os.path.join(os.environ["BFX_SECRETS_DIR"],"BFX_ADMIN_TOKEN")).read().strip()
     print(u.urlopen(u.Request("http://127.0.0.1:8080/admin/trading-status",headers={"Authorization":"Bearer "+t}),timeout=30).status)'
     ```

     真的跑一次 `sudo bfx-op halt` 只在排定的演練時做：它會讓交易停下，而且不能用 CLI 恢復。
   - Telegram：`sudo docker exec bfx-bot /app/.venv/bin/python -m scripts.send_test_alert` exit 0；
     funding 狀態：UI 的 funding 面板（webapi 用 `webapi/BFX_ADMIN_READ_TOKEN` 讀 bot）有資料。

5. 備份並移除 env 檔裡已搬走的 key，再 recreate：

   ```bash
   sudo install -d -o root -g root -m 0700 /root/pre-9h
   sudo cp -p /opt/bfx/runtime/bot.env /opt/bfx/runtime/webapi.env /root/pre-9h/
   sudo sed -i -E '/^(DATABASE_URL|BFX_ADMIN_TOKEN|BFX_ADMIN_READ_TOKEN|BFX_VAULT_KEK|TELEGRAM_BOT_TOKEN)=/d' /opt/bfx/runtime/bot.env
   sudo sed -i -E '/^(DATABASE_URL|BFX_ADMIN_READ_TOKEN)=/d' /opt/bfx/runtime/webapi.env
   sudo stat -c '%U %a %n' /opt/bfx/runtime/bot.env /opt/bfx/runtime/webapi.env   # 都要是 root 600
   sudo bfx-deploy --recreate
   ```

6. 核對 env 已經沒有秘密（只數 key 名稱，三個指令都必須印 0）：

   ```bash
   for c in bfx-bot bfx-webapi; do docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}' | cut -d= -f1 \
     | grep -cE '^(DATABASE_URL|BFX_ADMIN_TOKEN|BFX_ADMIN_READ_TOKEN|BFX_VAULT_KEK|TELEGRAM_BOT_TOKEN)$'; done
   docker logs --since 10m bfx-bot 2>&1 | grep -c secret_env_keys_ignored
   ```

回滾：
- 第 5 步之前，自動回滾到 9h 之前的版本照常可行，因為舊版讀的 env 還在；第 4 步的 exec 檢查失敗時也是走這條（revert）。
- 第 5 步之後，9h 之前的版本開不了機（env 裡沒有 `DATABASE_URL`）。要回到那種版本，先從 `/root/pre-9h/` 把那幾行補回
  env 檔，再部署。
- 9h 之後的 release 成為「上一個成功版本」時，`/root/pre-9h/` 就不再需要：`sudo rm -r /root/pre-9h`。

### Mirror（git fetch 走匿名 HTTPS；9h）

bfx-deploy 以 `ubuntu` 身分在 mirror `/home/ubuntu/bfx-funding-bot` 執行 `git fetch origin`。remote URL 在 VM 的 mirror 設定裡，
不在 repo 的 code 裡。repo 是 public，所以 fetch 走匿名 HTTPS：
- TLS 驗證的是 github.com 的身分，與 SSH host key 驗證等價；
- 不需要長期憑證；
- 部署只認 `origin/main` 上的 commit。

一次性切換（root；可逆，改回原本的 URL 即可）：

```bash
sudo -u ubuntu git -C /home/ubuntu/bfx-funding-bot remote get-url origin       # 記下原值（git@github.com:...）
sudo -u ubuntu git -C /home/ubuntu/bfx-funding-bot remote set-url origin https://github.com/Will413028/bfx-funding-bot.git
sudo -u ubuntu env GIT_TERMINAL_PROMPT=0 git -C /home/ubuntu/bfx-funding-bot fetch --dry-run origin main   # 必須 exit 0、不問密碼
```

下一次 bfx-deploy 成功後（`journalctl -u bfx-deploy` 沒有 `git_fetch_failed`）再移除 deploy key：
1. GitHub repo Settings → Deploy keys 刪掉 VM 那把；
2. VM 上刪掉 `ubuntu` 的那把私鑰，以及 `~/.ssh/config` 裡對應 github.com 的設定（先 `ls -l ~ubuntu/.ssh` 確認
   是哪個檔，只刪 deploy key 那一組）。

### Tailnet 入口（netns、tailscale）

compose 裡的 `netns`（`bfx-netns`）只持有 network namespace（只跑 `sleep`），`tailscale`（`bfx-tailscale`，
節點 `bfx-ops`、userspace、沒有 capability、沒有 port）以 `network_mode: service:netns` 加入它；9d-5 起
webapi 也加入它。tailscaled 重啟、崩潰或單獨重建都不會換掉 namespace（計畫 D13，holder spike 實測）。
兩者都要 compose healthcheck：`bfx-tailscale` 的 `/healthz` 在節點拿到 tailnet 位址後才回 200。
**入口的狀態不讓 bot 被停**：bot、webapi 照舊把關；入口（健康檢查，以及部署後檢查的 image、名稱、
namespace）只在這次部署新建或重建了它（第一次部署、image 或 serve.json 改了、被 §1 第 7 步強制重建）時才把關，
而且在 app 都通過之後才判斷，錯誤碼是 `ingress_unhealthy:<服務>[:<不符項>]`。這種失敗只在回滾真的做得到時才回滾（沒套用 migration、有上一個成功版本、上一版
的 schema 就是現在的 schema；回到沒有入口的版本會移除它）——例如 auth key 錯、tailnet policy 沒有 `tag:bfx-ops`
的 owner。回滾做不到時新版照樣留著（它的 app 是健康的），記 `deployed` 加 warning。其他情況（入口本來就壞、
這次沒動到它；回滾；`--recreate`；docker 答不出容器 Id）入口不健康只在 ledger detail 記
`warnings=ingress_unhealthy:<服務>` 並發 warning，bot 照跑。平常的部署不重建兩者（見 §1 第 7 步）。
已知例外（機率低）：Compose 本身起不了入口容器（`compose_up_failed`，分不出是哪個服務）照一般失敗處理；只因入口而
回滾、回滾卻在 schema 檢查之後（上一版的 `up` 或健康檢查）失敗時，照一般規則停 bot。
Serve 設定是 tracked 的 `deploy/vm/tailscale/serve.json`（443 → `127.0.0.1:8000`，轉送 operator capability），
隨部署寫進 release 目錄；內容改了那次部署會重建全部服務讓它生效。webapi 在同一個 namespace 只聽
`127.0.0.1:8000`，提供營運 UI 與 `/api/v1`（身分見 ARCHITECTURE「Web API identity」）。

- 查狀態：`docker exec bfx-tailscale tailscale status`（tagged、key 到期 none）、
  `docker exec bfx-tailscale tailscale serve status`。
- **不要單獨 `docker restart bfx-netns`**：holder 換了新 namespace，tailscale（與 9d-5 起的 webapi）會留在
  沒有 `eth0` 的舊 namespace，手動 `docker compose up` 也不修（Compose 沿用沒變的容器）。最後一次部署是
  `deployed`／`rolled_back` 時，bfx-deploy 下一輪（5 分鐘內）的 netns 修復會只重建卡住的加入者（條件見 §5）；
  要立刻修就 `sudo bfx-deploy --recreate`（一律重建全部服務）。
- 換節點（state 壞掉）：admin console 刪節點 → `docker rm -f bfx-tailscale`（還有容器引用時 volume 刪不掉）→
  `docker volume rm bfx_tsstate` → 放一把新 auth key →
  `--recreate`。

### 第一次啟用 provenance 驗證（9i，一次性，順序不能換）

不需要 GitHub token，也不需要把 GHCR package 改成 public。

1. 9i 的 PR merge；它自己的 release 由舊工具部署（舊工具不驗簽）。
2. 部署成功後照下方 §4 重跑一次 `install.sh`（裝釘版的 gh；新工具找不到 gh 時，下一個 release 會停在
   `attestation_verifier_missing`，fail closed，bot 與 webapi 不受影響）。
3. 對目前部署的 digest 手動重驗一次，印出 `verified:`、exit 0 才算完成：

   ```bash
   # bot 正在跑的 image 是 <repository>@sha256:<digest>，取 @ 後面那段
   D=$(sudo docker inspect bfx-bot --format '{{.Config.Image}}'); D=${D#*@}
   sudo bfx-deploy verify-provenance "$D"; echo "exit=$?"
   ```

   9i 的 release 本身就是 9i 的 `release.yml` 簽的，所以這一步驗的就是它。`NOT verified: attestation_unverified:no_attestation`
   表示該 release run 的 `Attest build provenance` step 沒成功，見 §5「Provenance 驗不過」。

## 4. 主機工具與 DR 腳本的版本

bfx-deploy 自己維護主機上的工具，不再依賴手動 `install.sh`。例外：bfx-deploy 不寫 `/usr/local/sbin`
的入口（`bfx-deploy`、`bfx-notify`、`bfx-op`）、`/etc/sudoers.d/bfx-op` 與釘版的 gh，所以 release 新增 sbin 入口、
改了 sudoers drop-in 或改了 `GH_VERSION`（`install.sh` 與 `bfx_deploy.py` 同時改）時，要在部署成功後重跑一次
`install.sh`（在那之前，新工具找不到它要的 gh，每個新 release 都以 `attestation_verifier_missing` 擋下）：

```bash
sudo systemctl stop bfx-deploy.timer
systemctl is-active bfx-deploy.service          # 必須是 inactive，否則等它跑完
git -C /home/ubuntu/bfx-releases/current rev-parse HEAD   # 等於 ledger 最新 deployed 的 revision
sudo /home/ubuntu/bfx-releases/current/deploy/vm/ops/install.sh
sudo systemctl start bfx-deploy.timer
```

從 DR worktree 跑（它就是已部署 revision 的乾淨 checkout），不要為此 checkout mirror。

| 路徑 | 內容 | 何時更新 |
|---|---|---|
| `/usr/local/lib/bfx-ops/releases/<rev>/{ops,systemd}` | 從 `<rev>` 的 git 物件寫出，root 擁有 | release 部署成功後 |
| `/usr/local/lib/bfx-ops/releases/<rev>/ops/.venv` | `uv sync --frozen`（`deploy/vm/ops/uv.lock`：PyYAML、pathspec） | 同上 |
| `/usr/local/lib/bfx-ops/gh/<版本>/bin/gh` | 驗 provenance 的 GitHub CLI，`install.sh` 依 `GH_VERSION`＋`GH_SHA256` 下載（不用 apt：unattended-upgrades 會繞過 7 天冷卻期） | **bfx-deploy 不裝**：升 gh 版本的 release 部署成功後重跑 `install.sh` |
| `/usr/local/lib/bfx-ops/current` → `releases/<rev>` | wrapper `bfx-deploy`／`bfx-notify`／`bfx-op` 與 unit 都執行這裡（含每週報告的 runner 與 `docker-compose.weekly-report.yml`） | 同上，**下一輪 bfx-deploy 才用新版** |
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
- 每週報告（`bfx-weekly-report.service`）的步驟也屬於主機工具：部署成功後下一次執行就用新步驟，
  image 是 ledger 的 backend digest。VM mirror 的 working tree 不再被任何排程讀取。
  手動執行與 timer 見 [operations §8](operations.md#8-定期與背景工作)。

## 5. 失敗與回滾

| ledger outcome | 意思 | 要做什麼 |
|---|---|---|
| `deployed` | 成功 | 無 |
| `failed`，detail `precheck:*` | 部署前檢查失敗，現行 release 沒動 | 依 code 修（env、registry、git、foreign container…），再 `--retry` |
| `failed`，`precheck:release_not_descendant_of_deployed` | `main` tag 指向比已部署 revision 舊（或不在其後）的 release，現行 release 沒動 | 不要 `--retry`；見下方「Provenance 驗不過」末段 |
| `failed`，`dr_checkout_failed` | 建不出目標的 DR checkout，現行 release 沒動 | 查 mirror／`/home/ubuntu/bfx-releases` 權限，再 `--retry` |
| `failed`，`webapi_stop_failed` | 有 migration 但停不了 webapi，什麼都沒做，bot 照跑 | 查 docker，再 `--retry` |
| `failed`，`bot_stop_failed` | 有 migration 但停不了 bot，什麼都沒做；detail 含 `webapi restarted` 表示剛停的 webapi 已重新啟動，含 `webapi_restart_failed:<why>` 表示 webapi 沒起來 | 查 docker，再 `--retry` |
| `failed`，`backup_failed` / `restore_test_failed(migration_pending)` / `migration_failed`，detail 含 `bot and webapi stopped until a release deploys` | schema 未變，**bot 與 webapi 已停**（UI 不可用） | 看 `journalctl -u 'bfx-restore-test@*'` 或 backup log；修好後 `--retry` 或出新 release |
| `failed`，`restore_test_failed(migration_pending):…:migration_rehearsal_failed`，detail 含 `bot restarted on the running release; webapi restarted on the running release` | 目標 release 的 migration 在隔離副本上失敗；prod schema 未變，**舊 bot 與舊 webapi 已重新啟動** | 看 `journalctl -u 'bfx-restore-test@*'` 的 `migration_rehearsal_failed: <step>`；修 migration 出新 release（不 `--retry` 同一 digest） |
| 同上但 detail 含 `bot_restart_failed:<why>` 或 `webapi_restart_failed:<why>` | 演練失敗，重新啟動的那個 container 起不來或不健康（含 settle 60 秒），已停；`BOT_STOP_FAILED, bot may be running`（或 `WEBAPI_…`）表示連停都停不了；另一個照 detail 各自記錄 | 查 `docker ps`、`docker logs bfx-bot`／`bfx-webapi`；修好後 `--recreate` 或出新 release |
| `failed`，`restore_test_failed(dr_paths:…)` | 沒有 migration 的 DR 變更沒通過 restore test，現行 release 沒動 | 修 DR 變更出新 release |
| `failed`，`migration_partial_or_unverified` | schema **已變**、狀態不明，bot 與 webapi 已停 | 見下方「migration 之後」 |
| `failed`，`ledger_started_unrecorded` | `started` 列寫不進去，container 沒動（有 migration 時 bot 與 webapi 已停：`bot and webapi stay stopped`） | 查 postgres，再 `--retry` |
| `rolled_back` | 新版不健康、沒有 migration，已回到上一個成功的 digest（用它原本的 deployment id） | 查原因；修好出新 release，或 `--retry` 同一 digest |
| `failed`，`rollback_failed:alembic_current_failed` 或 `rollback_failed:rollback_schema_mismatch` | 新版不健康，schema 已經不是上一個成功 release 的（之前的嘗試套用過 migration）：不回滾、什麼都沒建回來，bot 已停，webapi 維持新版 | roll forward（見下方「migration 之後」） |
| `failed`，`unhealthy:*; migrations applied` 或 `no previous deployment` | 不能自動回滾，bfx-deploy 已停 bot；有 migration 時 webapi 也停（`bot stopped; webapi stopped`） | 見下方 |
| 只有 `started`、沒有結束列 | 嘗試中途中斷（bfx-deploy 被殺、主機重開） | 看 `docker ps` 與 journal 判斷停在哪；之後 `--retry` |

**migration 之前**失敗：沒有 migration 的部署，現行 release 不受影響，自動回滾只發生在這種情況。
有 migration 的部署在備份前就停了 webapi 與 bot，失敗時兩者維持停止（Will 2026-09-25；全停 D6）。

**migration 之後**失敗：舊 code 可能不能在新 schema 上跑（live daemon 開機會比對
image 內 migrations 推導出的唯一 head（`core/schema_head.build_head`）與 `alembic_version`，不一致就發
`venue_offers_may_remain` 告警、拒絕開機，不寫交易狀態也不碰 venue；webapi 做同樣的比對，
不一致就拒絕開機）。只能：

1. roll forward：修正後出新 release（新 commit → CI → GHCR → bfx-deploy）；或
2. 把 migration 前的備份還原到**隔離**資料庫比對，再決定。**不要把舊備份蓋回 production**
   ——venue 可能已有寫入，見 [rollback after a venue write](rollback-after-venue-write.md)。

### Provenance 驗不過（discovery 失敗，不寫 ledger）

這些 code 記在 `/var/lib/bfx-deploy/discovery-failure.json` 與 `journalctl -u bfx-deploy`，連續 30 分鐘後
Telegram 發一次 warning（`bfx-deploy cannot identify or read the release … (<code>); no deploy is happening`），
恢復時發 info。**bot 與 webapi 不受影響**：驗不過的 image 從沒被 pull 或執行，現行 release 照跑。

| code | 意思 | 要做什麼 |
|---|---|---|
| `attestation_verifier_missing` | `/usr/local/lib/bfx-ops/gh/<GH_VERSION>/bin/gh` 不存在或跑不起來 | 照 §4 重跑 `install.sh`（它會依 sha256 裝上釘版 gh） |
| `attestation_verifier_wrong_version` | 該路徑的 gh 回報的版本不是 `GH_VERSION` | 同上；不要手動放別的版本 |
| `attestation_unverified:manifest_digest_mismatch` | registry 給的 manifest bytes 的 sha256 不是該 digest | 不正常：registry 或中間人改了內容。當成下方第 3 點處理 |
| `attestation_unverified:no_attestation` | GitHub attestation API 對這個 digest 回 404 或空清單 | 見下方第 2 點 |
| `attestation_unverified:rate_limited` | 匿名 GitHub API 的額度用完（403 且 `x-ratelimit-remaining: 0`，或 429）；journal 印出重置時間 | 什麼都不用做，額度重置後下一輪自動重驗。同一個 IP 上別的程式也在匿名打 GitHub API 時才可能發生 |
| `attestation_unverified:api_unreachable`／`api_unavailable` | 連不到 GitHub API，或它回了 200 以外（含非 rate limit 的 403）、或看不懂的內容 | 外部中斷，見下方第 1 點 |
| `attestation_unverified:bundle_unreadable` | 清單有 attestation，但沒有一筆 bundle 讀得出來（下載失敗、不是 https、超過 2 MiB、snappy 解不開、不是 JSON 物件）；journal 逐筆記原因 | 多半是外部中斷，下一輪重試；持續發生就看 journal 的原因 |
| `attestation_unverified:gh_rejected` | 每一筆 bundle gh 都拒絕（或 exit 0 卻沒回報通過的 attestation）；journal 逐筆記 gh 的 stderr | 見下方第 3 點 |
| `attestation_unverified:gh_unavailable` | `gh attestation verify` 逾時（180 秒）或跑不起來 | 多半是 Sigstore TUF CDN 中斷，見下方第 1 點 |

判斷方式（journal 的 `attestation N of M for … not accepted (exit …)` 後面是 gh 的 stderr）：

1. **外部中斷**（GHCR、GitHub API、attestation 下載點、Sigstore TUF CDN `tuf-repo-cdn.sigstore.dev` 連不到）：
   什麼都不用做，恢復後下一輪自動部署。中斷期間就是不部署，這是預期的 fail-closed；
   [GitHub status](https://www.githubstatus.com/) 與 [Sigstore status](https://status.sigstore.dev/) 可確認。
   不要為了趕部署關掉驗證：要繞過只能改 root 擁有的工具，這本身就是事故。
2. **這個 digest 沒有 attestation**：看 image label 的 `bfx.ci-run-url` 那次 release run 的
   `Attest build provenance` step。release 推完 image 才簽，所以剛推完的幾分鐘內可能先失敗一輪，之後自己好；
   若那個 step 失敗了，重跑該 release run 或推下一個 commit。
3. **簽章存在但身分不符**（stderr 是 `expected BuildSignerURI`、`expected SourceRepositoryRef`、self-hosted
   runner、repo 不符）、manifest 對不上 digest，或 `main` tag 指向一個 CI 從沒建過的 digest：
   **當成供應鏈事件處理**——有人用 `write:packages` 憑證推了 image。不要部署；撤銷可疑的 PAT／token，
   查 GHCR package 的版本歷史，重跑最新 main 的 release 把 `main` tag 推回正確的 digest。

手動重驗：`sudo bfx-deploy verify-provenance sha256:<digest>`。它跑的就是 bfx-deploy 第 3 步的同一段程式
（同一個釘版 gh、同一組 `--repo`／`--signer-workflow`／`--source-ref`、同樣用 `ghcr.env` 讀 manifest、不用任何
GitHub token），不寫 ledger、不動任何容器；通過印 `verified: <image>` 並 exit 0，否則印
`NOT verified: <code>`（code 同上表）並 exit 1。

降版（`precheck:release_not_descendant_of_deployed`）走的是 ledger：寫一列 `failed`、發 critical、現行 release
沒動，同一個 digest 之後不會再試。代表 `main` tag 指向的 revision 不在已部署 revision 之後；正常的 release
流程不會發生（CI 只在 head 時推 `main`），要退回舊行為請推 revert commit（roll forward）。查 GHCR 的 `main`
tag 是誰推的，處理方式同上面第 3 點。

### 加入者留在 holder 已死的 network namespace

holder（`bfx-netns`）單獨重啟或單獨重建後，加入它的容器（tailscale；9d-5 起還有 webapi）會留在舊的
namespace：tailscale 離開 tailnet，webapi 的 `/health` 仍回 ok 但連不到 DB、也收不到 Serve 流量（9d-0 與
holder spike 實測），Docker 不會自己修。tailscale 自己重啟不會造成這件事（它只是加入者）。bfx-deploy 每一輪
沒有要部署時（up to date、already attempted），若最後一次嘗試是 `deployed` 或 `rolled_back`，就
`docker inspect bfx-netns bfx-tailscale bfx-webapi`：在跑、`NetworkMode` 是 `container:` 的加入者，只要它的
`NetworkMode` 不是目前 holder 的 Id、或 holder 比它晚啟動，就以最後成功部署的 digest 與 deployment id 只重建
這些加入者（`up --no-deps --force-recreate <它們>`），再等各自的健康檢查（webapi `/ready`、tailscale 的 compose
healthcheck）與 settle。
不看健康狀態（DB 短暫中斷不會觸發）、永遠不碰 bot、不寫 ledger，只發通知（成功 info、失敗 warning）。
同一個事件（holder Id＋啟動時間）連續失敗 3 次發一次 critical、之後不再重試，直到 namespace 恢復正常
或 holder 又重啟；計數在 `/var/lib/bfx-deploy/netns-repair.json`，要立刻重試就刪掉它。
holder 沒在跑、加入者沒在跑或不在 holder 的 namespace（9d-5 之前的 webapi）、最後一次嘗試是
`failed`／`started` 時什麼都不做。

bot 停下時 venue 上的掛單不會自己消失：停 bot 之前，若有掛單而且停機會超過幾分鐘，先在 UI 或
`sudo bfx-op halt --reason "…"` 執行 kill（見 operations §3）。bot 停著時兩者都不可用，改在 Bitfinex 網頁手動撤單並記錄。

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
