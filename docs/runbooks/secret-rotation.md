# 秘密輪替

每把秘密怎麼換、怎麼驗證、換的時候會停多久。檔案位置與權限規則見
[deploy「秘密檔」](deploy.md#秘密檔botwebapi9h) 與 [deploy「Env 檔規則」](deploy.md#env-檔規則)。

## 何時換

由事件觸發，不排定期輪替（品質計畫 9h，D15）。唯一的例外是 `attest.env` 的 GitHub token，它每年到期，見文末。
符合任一條件就換：

- 疑似外洩：
  - 值出現在 log、chat、issue、截圖、終端機錄影或 AI agent 的輸出裡；
  - 備份或磁碟映像落到預期外的地方；
  - 服務商通知外洩。
- 裝置或人員變動：
  - 曾經登入 VM 或持有這把秘密的裝置遺失、送修或汰換；
  - 有人不再需要存取。
- 換主機：新主機一律換新值，不沿用舊主機上的值。

只換受影響的那幾把；不確定影響範圍時，整張表從上到下換一次。

## 通用寫法

- 值只存在 shell 變數裡，不要 `echo`、不要當參數傳給其他程式。`printf` 是 bash 內建指令，不會出現在行程列表裡。
- 秘密檔一律用 `install` 從 stdin 寫入（`root:4600 0640`），寫完用 `stat` 核對，不要 `cat`：

  ```bash
  V=$(openssl rand -hex 32)                      # 新的隨機 token；DB 密碼也用這個
  printf '%s' "$V" | sudo install -o root -g 4600 -m 0640 /dev/stdin /opt/bfx/runtime/secrets/<service>/<KEY>
  sudo stat -c '%U:%g %a %s %n' /opt/bfx/runtime/secrets/<service>/<KEY>   # root:4600 640 <大於 0>
  unset V
  ```

- env 檔（root 0600）裡的 key 用 awk 改寫，值經環境變數交給 awk，不放在命令列參數上：

  ```bash
  sudo env V="$V" awk -v k=<KEY> 'BEGIN{FS=OFS="="} $1==k{print k "=" ENVIRON["V"]; next} {print}' \
    /opt/bfx/runtime/<file>.env | sudo install -o root -g root -m 0600 /dev/stdin /opt/bfx/runtime/<file>.env.new
  sudo mv /opt/bfx/runtime/<file>.env.new /opt/bfx/runtime/<file>.env
  ```

- `install` 與 `mv` 會換掉檔案（新的 inode）。秘密檔是以 bind mount 掛進容器的，跑中的容器在 recreate 之前仍然看到舊檔。
  所以每次換完都要 recreate；KEK 是例外，見下面的 KEK 一節。
- 改了 bot／webapi 讀的東西後，跑 `sudo bfx-deploy --recreate`（[operations §7](operations.md#7-改了-runtime-env-之後重建-container)）：
  - bot 收 SIGTERM 會乾淨停機，這是正常流程；
  - recreate 會重建全部服務，約一分鐘；
  - recreate 不會回滾，不健康時 bot 會停下：修好檔案後再跑一次 recreate。
- 換完的驗證，三項都要成立：
  - ledger 最新列是 `deployed`；
  - `sudo bfx-op status` 正常；
  - Telegram 沒有 `boot_refused`。

## 資料庫 role 密碼

PostgreSQL 每個 role 只有一個密碼。`ALTER ROLE` 之後，已經連上的連線繼續可用，新的連線立刻要用新密碼。

順序固定是：**先寫好新的秘密檔 → `ALTER ROLE` → recreate**。照這個順序，空窗只有「ALTER 到 recreate 完成」這一段：
- 這段時間裡 bot 新開的 DB 連線會失敗，已經開著的連線不受影響；
- 第一步寫好檔案之後到 ALTER 之前，bot 若剛好重啟，會用新密碼連線而失敗。所以三步要連續做完。

| role | 存放位置 | 換完之後 |
|---|---|---|
| `bfx_bot` | `secrets/bot/DATABASE_URL` | `sudo bfx-deploy --recreate` |
| `bfx_webapi` | `secrets/webapi/DATABASE_URL` | `sudo bfx-deploy --recreate` |
| `bfx_operator` | `operator.env` 的 `DATABASE_URL` | 不用重建，`bfx-op` 每次都重讀 |
| `bfx_research` | `research.env` 的 `DATABASE_URL` | 不用重建，下一次每週報告或研究容器就用新值 |
| `bfx_sim` | `sim.env`（只在 soak 期間存在） | 照 [simulation-soak](simulation-soak.md) 重寫 `sim.env` |
| owner（`POSTGRES_USER`） | `migrate.env` 的 `DATABASE_URL` | 不用重建。`.env.postgres.runtime` 的 `POSTGRES_PASSWORD` 只在空 PGDATA 第一次 initdb 時使用，改 owner 密碼**不需要**動它 |

以 `bfx_bot` 為例（root shell，`<owner>` 見 `AGENTS.local.md`）：

```bash
NEW=$(openssl rand -hex 32)
# 1. 新的秘密檔：原 URL 只換密碼段（host、scheme、query 照舊）
sudo env NEW="$NEW" awk '{ if (!sub(/:\/\/bfx_bot:[^@]*@/, "://bfx_bot:" ENVIRON["NEW"] "@")) exit 1; printf "%s", $0 }' \
  /opt/bfx/runtime/secrets/bot/DATABASE_URL \
  | sudo install -o root -g 4600 -m 0640 /dev/stdin /opt/bfx/runtime/secrets/bot/DATABASE_URL.new
sudo stat -c '%U:%g %a %s' /opt/bfx/runtime/secrets/bot/DATABASE_URL.new      # root:4600 640 <大於 0>
sudo mv /opt/bfx/runtime/secrets/bot/DATABASE_URL.new /opt/bfx/runtime/secrets/bot/DATABASE_URL
# 2. ALTER ROLE：SQL 從 stdin 進去，密碼不出現在命令列，也不寫進 PG log
printf "SET log_statement='none'; SET log_min_error_statement='panic'; ALTER ROLE bfx_bot PASSWORD '%s';\n" "$NEW" \
  | sudo docker exec -i --user postgres bfx-postgres psql -U <owner> -d bfx -v ON_ERROR_STOP=1 -q
unset NEW
# 3. 重建
sudo bfx-deploy --recreate
```

- awk 沒有改到任何東西（URL 的形式不符）時 exit 1、什麼都不寫：先停下，不要做 ALTER。
- `bfx_webapi` 把路徑換成 `secrets/webapi/DATABASE_URL`、role 換成 `bfx_webapi`，其餘相同。
- env 檔裡的 role 用「通用寫法」的 awk 改 `DATABASE_URL` 那一行，再做第 2 步，不需要第 3 步。
- 驗證：
  - recreate 之後 ledger 是 `deployed`；
  - `docker logs --since 5m bfx-bot 2>&1 | grep -c 'password authentication failed'` 印 0。
- one-shot 的 role 用它自己的路徑實際連一次：
  - `bfx_operator`：`sudo bfx-op status`；
  - `bfx_research`：照 [research-one-shot-jobs](research-one-shot-jobs.md) 跑一次連線檢查。

## Admin tokens

| key | 存放位置 | 用途 |
|---|---|---|
| `BFX_ADMIN_TOKEN` | `secrets/bot/BFX_ADMIN_TOKEN` | bot 的 `/admin/halt`（`bfx-op halt` 從 bot 容器內讀這個檔）以及兩個唯讀端點 |
| `BFX_ADMIN_READ_TOKEN` | `secrets/bot/BFX_ADMIN_READ_TOKEN` **與** `secrets/webapi/BFX_ADMIN_READ_TOKEN`，兩檔同值 | webapi 讀 bot 的 funding 狀態；bot 拒絕用它 halt |

1. 用「通用寫法」產生新值，寫進檔案。`BFX_ADMIN_READ_TOKEN` 要在同一個 shell 裡寫兩個檔，而且不能等於 `BFX_ADMIN_TOKEN`。
   兩者相同時 bot 會丟掉 read token，webapi 讀狀態會被拒。
2. `sudo bfx-deploy --recreate`。停機約一分鐘，就是 recreate 本身。
3. 驗證：
   - admin token：照 [deploy 9h 切換第 4 步](deploy.md#9h-切換一次性依序在-merge-9h-的-pr-之前做第-13-步)，用唯讀端點確認 bot 收這個檔的 token（印 200），不要真的 halt；
   - read token：UI 的 funding 面板有資料（webapi 拿得到 bot 的狀態）。

## Telegram bot token

BotFather 的 `/revoke` 會讓舊 token 立刻失效，所以兩條告警路徑要一起換：
- bot 內的 sink：`secrets/bot/TELEGRAM_BOT_TOKEN`；
- 主機工具 `bfx-notify`：`notify.env` 的 `TELEGRAM_BOT_TOKEN`。

`TELEGRAM_CHAT_ID` 不是秘密，留在 `bot.env`／`notify.env`。

1. BotFather `/revoke` 取得新 token。把它讀進變數，不回顯：`read -rs V`。
2. 用「通用寫法」寫 `secrets/bot/TELEGRAM_BOT_TOKEN`，再用 awk 改 `notify.env` 的 `TELEGRAM_BOT_TOKEN`，最後 `unset V`。
3. `sudo bfx-deploy --recreate`。
4. 驗證：
   - `sudo bfx-notify --level info "token rotated"` 收得到；
   - `sudo docker exec bfx-bot /app/.venv/bin/python -m scripts.send_test_alert` exit 0。

從 revoke 到第 3 步完成之間，bot 的告警送不出去，只記 log。

## R2（offsite backup）

照 [offsite-dr「Rotation and incident posture」](offsite-dr.md#rotation-and-incident-posture)：
- 換的期間系統維持 halt；
- 只改 VM 的 secret fragment，重跑 validator、archive check、backup preflight 與 isolated restore；
- 新的證據通過之後，才撤銷舊 token。

repository cipher 的輪替是另一個 repository migration，要保留讀舊備份需要的每一個 passphrase。

## Bitfinex API key

只有一條路：[operations §11](operations.md#11-換交易所憑證) 的 `sudo bfx-op rotate-credential`。
- bot 驗證新 key 後，用 KEK 加密存進 DB，然後自動重啟；
- 確認 active 憑證是新 id 之後，才到 Bitfinex 刪舊 key。

## Vault KEK（`BFX_VAULT_KEK`）

ADR 2026-08-31 Amendment D5'：不做 re-wrap migration。換 KEK 的做法是產生新 KEK，同時換一把新的 Bitfinex key，新 key 用新 KEK 加密存進 DB。舊 KEK 加密的舊憑證列已經退役（不再 active），之後解不開也沒關係。

可行的原因：bot 只在開機時（解 active 憑證）與換憑證時（加密新 key）讀 KEK，每次都是當下讀檔（`load_kek()`）。

1. 前提同 [operations §11](operations.md#11-換交易所憑證)：
   - 已在 Bitfinex 建好新 key，權限與現行相同；
   - bot 這次開機已滿 10 分鐘；
   - 沒有部署正在進行。
2. 先把舊 KEK 留一份，萬一第 3–4 步之間 bot 重啟，可以放回去：

   ```bash
   sudo install -d -o root -g root -m 0700 /root/kek-rotation
   sudo cp -p /opt/bfx/runtime/secrets/bot/BFX_VAULT_KEK /root/kek-rotation/
   ```

3. 寫新 KEK（32 bytes，base64）。這裡要**原地覆寫**（`tee`，同一個 inode），跑中的 bot 才讀得到新 KEK：
   用 `install` 會換成新的 inode，跑中的容器仍然看到舊 KEK，新 key 就會被舊 KEK 加密，而重啟後讀到的是新 KEK，於是解不開。

   ```bash
   NEWKEK=$(openssl rand -base64 32)
   printf '%s' "$NEWKEK" | sudo tee /opt/bfx/runtime/secrets/bot/BFX_VAULT_KEK > /dev/null
   unset NEWKEK
   sudo stat -c '%U:%g %a %s' /opt/bfx/runtime/secrets/bot/BFX_VAULT_KEK          # root:4600 640 44
   # 容器看到的必須是新檔（比對雜湊，不印值）；兩行要相同，不同就停下並照下面的方式放回舊 KEK
   sudo sha256sum /opt/bfx/runtime/secrets/bot/BFX_VAULT_KEK | cut -d' ' -f1
   sudo docker exec bfx-bot /app/.venv/bin/python -c \
     'import hashlib;print(hashlib.sha256(open("/run/secrets/BFX_VAULT_KEK","rb").read()).hexdigest())'
   ```

   從這一刻到第 4 步 `applied`，bot 不能重啟：重啟後它會用新 KEK 解舊憑證，解不開，於是拒絕開機（`boot_refused`，
   credential vault cannot be opened）。真的發生時：
   - 把舊 KEK 原地寫回去：`sudo tee /opt/bfx/runtime/secrets/bot/BFX_VAULT_KEK < /root/kek-rotation/BFX_VAULT_KEK > /dev/null`；
   - 再 `--recreate`；
   - 然後從第 2 步重來。
4. 立刻送出換憑證：`sudo bfx-op rotate-credential --reason "kek rotation" --label "YYYY-MM-DD"`。
   bot 會用新 KEK 加密新 key，`applied` 後自動重啟，開機時用新 KEK 解開新憑證。
5. 照 operations §11 第 2 步驗證，三項都要成立：
   - active 憑證是新 id；
   - bot 的 `StartedAt` 晚於 applied 的時間；
   - `bfx-op status` 正常。
   都成立之後：
   - `sudo shred -u /root/kek-rotation/BFX_VAULT_KEK && sudo rmdir /root/kek-rotation`；
   - 到 Bitfinex 刪舊 key。

舊 KEK 若是因為疑似外洩而換：
- 舊 key 要刪掉，因為它的密文在備份裡、可以用舊 KEK 解開。這一步不能省。
- 換完後的第一個 full backup 之前的備份，仍然可以用舊 KEK 解出舊 key。舊 key 刪掉之後，這些密文就沒有用了。

## `SENTRY_DSN`

不是秘密，留在 `bot.env`／`webapi.env`：
- DSN 的 key 本來就隨每個 event 的 envelope header 送出；
- 前端版本以 `NEXT_PUBLIC_SENTRY_DSN` 打包在 static export 裡；
- 外洩最多讓人灌垃圾事件，緩解方式是 Sentry 專案的 Allowed Domains 與 rate limit。

log 與 Telegram 仍然會遮蔽它。真的要換：
1. 在 Sentry 專案停用舊的 client key、建立新的；
2. 改兩個 env 檔與 repository variable `NEXT_PUBLIC_SENTRY_DSN`（`BFX_FE_SENTRY_DSN` 要同值）；
3. `--recreate`，並重發 release。

## 其他

- `TS_AUTHKEY`（`tailscale.env`）：只在節點第一次登入時使用；節點已登入時可以是空檔。換節點見
  [deploy「Tailnet 入口」](deploy.md#tailnet-入口netnstailscale)。
- `ghcr.env`（GHCR PAT）：9i 確認驗證 provenance 不需要 token 之後，刪檔並在 GitHub 撤銷 PAT（見 deploy「Env 檔規則」）。
  在那之前要換，就到 GitHub 重發 `read:packages` 的 classic PAT，用 awk 改 `GHCR_TOKEN`。下一輪 bfx-deploy 就會用新值。
- mirror 的 deploy key：9h 起 mirror 改用匿名 HTTPS，deploy key 應該已刪除（[deploy「Mirror」](deploy.md#mirrorgit-fetch-走匿名-https9h)）。
- `attest.env`（9i 的 GitHub token，fine-grained、只讀公開資料、不授予任何權限）：每年到期前換一次：
  1. 在 GitHub 建新 token，用 awk 寫進 `attest.env`；
  2. 下一輪 bfx-deploy 驗證 provenance 成功之後，撤銷舊 token。
