# 秘密輪替

每把秘密怎麼換、怎麼驗證、換的時候會停多久。檔案位置與權限規則見
[deploy「秘密檔」](deploy.md#秘密檔botwebapi9h) 與 [deploy「Env 檔規則」](deploy.md#env-檔規則)。

## 何時換

由事件觸發，不排定期輪替。
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

**規則：秘密的值不出現在任何行程的 argv 上，也不進 shell history。**

- 為什麼：`sudo` 會把整行指令（`COMMAND=...`）寫進 auth.log／journald，`ubuntu`（adm group）讀得到；
  `sudo`、`env`、`awk -v`、`sed` 的 expression 與任何外部程式的參數，在行程列表（`ps`、`/proc/<pid>/cmdline`）裡也看得到。
- 所以每個換秘密的步驟都在一個 root shell 裡做完：`sudo bash -euo pipefail <<'SH' … SH`。heredoc 的 `'SH'` 有引號，
  外層 shell 不展開裡面的任何東西，sudo 記下的只有 `bash -euo pipefail`。
- 值只在這個 root shell 裡出現：當場產生（`openssl rand`）、從終端機讀（`IFS= read -rs V < /dev/tty`，不回顯；
  stdin 已經是 heredoc，所以一定要 `< /dev/tty`），或從現有的 root-only 檔案取出。之後只經 stdin、pipe 或檔案傳遞：
  bash 內建的 `printf` 不開新行程，可以帶值；awk 用 `ENVIRON["V"]` 取值（`export V`），不用 `-v`。
- 不要把值打在指令列上（`V=...`、`psql -c "...PASSWORD..."`、互動式 psql 會寫 history），也不要 `echo` 或 `cat` 出來。
- 換檔一律：新內容寫進**同一個目錄**的暫存檔 → 產生步驟成功、而且暫存檔不是空的 → 才 `mv` 蓋過原檔。
  任何一步失敗就刪掉暫存檔並中止，原檔不動。不要把產生步驟直接 pipe 給 `install /dev/stdin <原檔>`：
  pipe 前段失敗時 `install` 照樣建出空檔、回 0。

範本（換一個秘密檔，或 env 檔裡的一個 key）：

```bash
sudo bash -euo pipefail <<'SH'
umask 077
# swap <檔案> <owner:group> <mode> <指令…>：指令的輸出寫進同目錄的暫存檔；指令失敗或輸出是空的就刪暫存檔、中止（原檔不動）
swap() {
  local file=$1 own=$2 mode=$3 tmp; shift 3
  tmp=$(mktemp "$file.XXXXXX")
  if ! "$@" > "$tmp" || [ ! -s "$tmp" ]; then rm -f "$tmp"; echo "STOP: $file untouched" >&2; exit 1; fi
  chown "$own" "$tmp"; chmod "$mode" "$tmp"; mv "$tmp" "$file"
  stat -c '%U:%g %a %s %n' "$file"
}
# setkey <env 檔> <KEY>：env 檔（root 0600）裡 KEY 那一行的值換成 $V；KEY 不是剛好一行就中止
setkey() {
  swap "$1" root:root 0600 awk -v k="$2" \
    'BEGIN{FS="="} $1==k{print k "=" ENVIRON["V"]; n++; next} {print} END{exit (n != 1)}' "$1"
}
export V
V=$(openssl rand -hex 32)          # 新的隨機 token；外部給的值改成：IFS= read -rs V < /dev/tty
swap /opt/bfx/runtime/secrets/<service>/<KEY> root:4600 0640 printf '%s' "$V"   # 秘密檔：印 root:4600 640 <大於 0>
# env 檔的 key 改成：setkey /opt/bfx/runtime/<file>.env <KEY>
SH
```

- `mv` 會換掉檔案（新的 inode）。秘密檔是以 bind mount 掛進容器的，跑中的容器在 recreate 之前仍然看到舊檔。
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

順序固定是：**新內容寫進暫存檔 → `ALTER ROLE` → 換上新檔 → recreate**，前三步在同一個 root shell 裡連續做完：
- 暫存檔寫不出來（URL 形式不符）或 `ALTER ROLE` 失敗時，刪掉暫存檔、中止：檔案與 DB 密碼都沒變。
- 換上新檔之後到 recreate 完成之前，跑中的 bot（或 webapi）還拿著舊密碼：已經開著的連線不受影響，新開的連線會失敗。
  所以 recreate 緊接著做。**recreate 的 precheck 擋下時（ledger `failed`、`precheck:*`），先把它修好再做任何其他事**，
  然後再跑一次 recreate；在那之前 bot 每開一條新連線都會失敗。

| role | 存放位置 | 換完之後 |
|---|---|---|
| `bfx_bot` | `secrets/bot/DATABASE_URL` | `sudo bfx-deploy --recreate` |
| `bfx_webapi` | `secrets/webapi/DATABASE_URL` | `sudo bfx-deploy --recreate` |
| `bfx_operator` | `operator.env` 的 `DATABASE_URL` | 不用重建，`bfx-op` 每次都重讀 |
| `bfx_research` | `research.env` 的 `DATABASE_URL` | 不用重建，下一次每週報告或研究容器就用新值 |
| `bfx_sim` | `sim.env`（只在 soak 期間存在） | 照 [simulation-soak](simulation-soak.md) 重寫 `sim.env` |
| owner（`POSTGRES_USER`） | `migrate.env` 的 `DATABASE_URL` | 不用重建。`.env.postgres.runtime` 的 `POSTGRES_PASSWORD` 只在空 PGDATA 第一次 initdb 時使用，改 owner 密碼**不需要**動它 |

以 `bfx_bot` 為例（`<owner>` 見 `AGENTS.local.md`；其他 role 只改第一行的三個值，見下方）：

```bash
sudo bash -euo pipefail <<'SH'
role=bfx_bot f=/opt/bfx/runtime/secrets/bot/DATABASE_URL owner=<owner>
umask 077
export V; V=$(openssl rand -hex 32)     # 只有 [0-9a-f]：放進 URL 不用編碼，也不含 awk sub() 的特殊字元 & 與 \
case $f in
  *.env) own=root:root mode=0600        # env 檔：只改 DATABASE_URL= 那一行
         prog='/^DATABASE_URL=/{n += sub("://" role ":[^@]*@", "://" role ":" ENVIRON["V"] "@")} {print} END{exit (n != 1)}' ;;
  *)     own=root:4600 mode=0640        # 秘密檔：整個檔就是 URL
         prog='{n += sub("://" role ":[^@]*@", "://" role ":" ENVIRON["V"] "@"); printf "%s", $0} END{exit (n != 1 || NR != 1)}' ;;
esac
tmp=$(mktemp "$f.XXXXXX")
fail() { rm -f "$tmp"; echo "STOP: $1; $f and the role password are unchanged" >&2; exit 1; }
# 1. 新內容寫進同目錄的暫存檔：只換密碼段（host、scheme、query 照舊）
if ! awk -v role="$role" "$prog" "$f" > "$tmp" || [ ! -s "$tmp" ]; then fail "URL not rewritten"; fi
chown "$own" "$tmp"; chmod "$mode" "$tmp"
# 2. ALTER ROLE：SQL 由 bash 內建的 printf 經 stdin 進 psql，密碼不上 argv，也不寫進 PG log
if ! printf "SET log_statement='none'; SET log_min_error_statement='panic'; ALTER ROLE %s PASSWORD '%s';\n" "$role" "$V" \
     | docker exec -i --user postgres bfx-postgres psql -U "$owner" -d bfx -v ON_ERROR_STOP=1 -q; then
  fail "ALTER ROLE failed"
fi
# 3. 換上新檔
mv "$tmp" "$f"
stat -c '%U:%g %a %s %n' "$f"            # 秘密檔 root:4600 640 <大於 0>；env 檔 root:0 600
SH
sudo bfx-deploy --recreate               # 4. bot／webapi 才需要；precheck 擋下就先修好，見上
```

- 印出 `STOP: URL not rewritten`：檔案裡沒有剛好一個 `://<role>:<密碼>@`，什麼都沒寫、DB 也沒動。先查 URL 的形式。
- 印出 `STOP: ALTER ROLE failed`：暫存檔已刪、原檔沒動。若失敗的原因可能發生在 ALTER 已經生效之後（例如連線中斷），
  整段再跑一次：它會產生新密碼、重新 ALTER、再換檔，檔案與 DB 就會一致。
- `bfx_webapi`：第一行改成 `role=bfx_webapi f=/opt/bfx/runtime/secrets/webapi/DATABASE_URL`。
- env 檔裡的 role：`f` 換成表中的 env 檔（例如 `role=bfx_operator f=/opt/bfx/runtime/operator.env`；owner 是
  `role=<owner> f=/opt/bfx/runtime/migrate.env`），不需要第 4 步。
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

1. 用「通用寫法」的範本，最後的 `swap` 那一行換成要換的檔案：
   - `BFX_ADMIN_TOKEN`：`swap /opt/bfx/runtime/secrets/bot/BFX_ADMIN_TOKEN root:4600 0640 printf '%s' "$V"`；
   - `BFX_ADMIN_READ_TOKEN`：同一個 root shell 裡、同一個 `V` 寫兩行，`secrets/bot/BFX_ADMIN_READ_TOKEN` 與
     `secrets/webapi/BFX_ADMIN_READ_TOKEN`。

   兩把 token 各自由 `openssl rand` 產生，所以不會相同（相同時 bot 會丟掉 read token，webapi 讀狀態會被拒）。
2. `sudo bfx-deploy --recreate`。停機約一分鐘，就是 recreate 本身。
3. 驗證：
   - admin token：照 [deploy 9h 切換第 4 步](deploy.md#9h-切換一次性依序在-merge-9h-的-pr-之前做第-13-步)，用唯讀端點確認 bot 收這個檔的 token（印 200），不要真的 halt；
   - read token：UI 的 funding 面板有資料（webapi 拿得到 bot 的狀態）。

## Telegram bot token

BotFather 的 `/revoke` 會讓舊 token 立刻失效，所以兩條告警路徑要一起換：
- bot 內的 sink：`secrets/bot/TELEGRAM_BOT_TOKEN`；
- 主機工具 `bfx-notify`：`notify.env` 的 `TELEGRAM_BOT_TOKEN`；
- GitHub Actions 的每日合約檢查（`venue-contract.yml`）：repository secret `TELEGRAM_BOT_TOKEN`。

`TELEGRAM_CHAT_ID` 不是秘密，留在 `bot.env`／`notify.env`。

1. BotFather `/revoke` 取得新 token。
2. 用「通用寫法」的範本，把 `V=$(openssl rand ...)` 與最後的 `swap` 換成下面幾行；跑到 `read` 時貼上新 token、按 Enter（不回顯）：

   ```bash
   IFS= read -rs V < /dev/tty
   [ -n "$V" ] || { echo "STOP: empty token" >&2; exit 1; }
   swap /opt/bfx/runtime/secrets/bot/TELEGRAM_BOT_TOKEN root:4600 0640 printf '%s' "$V"
   setkey /opt/bfx/runtime/notify.env TELEGRAM_BOT_TOKEN
   ```

3. `sudo bfx-deploy --recreate`。
4. GitHub secret 從 VM 的新檔直接 pipe，不經過終端機：在 Mac 上跑
   `tailscale ssh ubuntu@<vm-node> 'sudo cat /opt/bfx/runtime/secrets/bot/TELEGRAM_BOT_TOKEN' | gh secret set TELEGRAM_BOT_TOKEN -R Will413028/bfx-funding-bot`。
5. 驗證：
   - `sudo bfx-notify --level info "token rotated"` 收得到；
   - `sudo docker exec bfx-bot /app/.venv/bin/python -m scripts.send_test_alert` exit 0。

從 revoke 到第 3 步完成之間，bot 的告警送不出去，只記 log。

## Cloudflare Pages token（公開站）

`/opt/bfx/runtime/secrets/public-site/CLOUDFLARE_API_TOKEN`（`root:root 0600`），只有 `bfx-public-site.service` 讀
（systemd 以 `LoadCredential=` 交給上傳步驟，每次執行都重讀，換檔後不用 reload）。
權限是帳戶層級的 **Pages Write**（Cloudflare 沒有只限一個 Pages 專案的權限），並限定 VM 的出口 IP；
所以它也能改同帳戶的其他 Pages 專案，外洩時要先撤銷。VM 換公網 IP 後上傳會被拒（`upload_token_failed:http_403`），也要照這節重發。

1. Cloudflare dashboard → My Profile → API Tokens → Create Custom Token：Account → Cloudflare Pages → Edit，
   Account Resources 只選這個帳戶，Client IP Address Filtering 填 VM 的出口 IP（`curl -4 ifconfig.me`）。
2. 在 VM 用「通用寫法」的範本，把 `V=$(openssl rand ...)` 與最後的 `swap` 換成下面幾行；跑到 `read` 時貼上新 token、按 Enter（不回顯）。
   第一次（新主機）的 `install -d` 與 `public-site.env` 兩行也照做（先把 `<帳戶 id>` 換成 Cloudflare dashboard 首頁右側的 Account ID；這一行只在檔案不存在時寫入，寫錯了要直接改檔），之後輪替時已存在、不影響：

   ```bash
   install -d -m 0700 -o root -g root /opt/bfx/runtime/secrets/public-site
   [ -e /opt/bfx/runtime/public-site.env ] || printf 'BFX_PAGES_ACCOUNT_ID=<帳戶 id>\nBFX_PAGES_PROJECT=borrowrate\n' > /opt/bfx/runtime/public-site.env
   IFS= read -rs V < /dev/tty
   [ -n "$V" ] || { echo "STOP: empty token" >&2; exit 1; }
   swap /opt/bfx/runtime/secrets/public-site/CLOUDFLARE_API_TOKEN root:root 0600 printf '%s' "$V"
   ```

3. 驗證：`sudo systemctl start bfx-public-site.service`（會重新產生再上傳），journal 最後一行是 `published 6 files`。
4. 在 dashboard 撤銷舊 token。

## R2（offsite backup）

照 [offsite-dr「Rotation and incident posture」](offsite-dr.md#rotation-and-incident-posture)：
- 換的期間系統維持 halt；
- 只改 VM 的 secret fragment，重跑 validator、archive check、backup preflight 與 isolated restore；
- 新的證據通過之後，才撤銷舊 token。

### repository cipher passphrase（`repo1-cipher-pass`）

pgBackRest 不能替既有 repository 換 passphrase：已經寫進 R2 的備份只用舊的那把打得開。所以「換」等於
另開一個 repository 再退役舊的。以下程序寫好但**還沒演練過**，第一次照做時要邊做邊修正這一段：

1. 系統維持 halt。照 [offsite-dr](offsite-dr.md) 開新的 repository（新的 `repo1-path` 或新 bucket），新 passphrase 用
   `openssl rand -base64 48` 產生（精靈要求新的至少 32 字），先放進離線保管，再寫進 `r2.conf`。
2. 在新 repository 跑 `stanza-create`、`check`、一次 full backup 與 isolated restore test；全部通過前不動舊的。
3. 舊 repository 與它的 passphrase 保留到新 repository 的保留期涵蓋同樣的時間範圍（`repo1-retention-full=4` 個 full 之後），
   期間兩把 passphrase 都在離線保管裡；之後刪除舊 repository，最後才銷毀舊 passphrase。

## 秘密的持有者

| 秘密 | VM 上的位置與權限 | VM 以外的副本 |
|---|---|---|
| bot／webapi 的秘密檔（DB 密碼、admin token、Telegram token、KEK） | `/opt/bfx/runtime/secrets/<service>/`，`root:4600 0640`（[deploy「秘密檔」](deploy.md#秘密檔botwebapi9h)） | 不保留副本，外洩或遺失就照本頁重發；KEK 遺失時舊憑證列解不開，照「Vault KEK」一節換一把新的 Bitfinex key（ADR 2026-08-31 D5'） |
| 主機工具與一次性工作的 env 檔（`notify.env`、`research.env`、`ghcr.env`、`public-site.env`、`tailscale.env`） | `/opt/bfx/runtime/`，root 0600 | 不保留 |
| Cloudflare Pages token | `/opt/bfx/runtime/secrets/public-site/CLOUDFLARE_API_TOKEN`，`root:root 0600` | 不保留（Cloudflare dashboard 可重發） |
| R2 S3 憑證 | `/home/ubuntu/bfx/pgbackrest/conf.d/r2.conf`，`ubuntu:70 0640`（70 是容器裡的 postgres） | 不保留（R2 dashboard 可重發） |
| `repo1-cipher-pass` | 同上 | **必須**離線保管（遺失＝備份全部打不開，`offsite-dr.md:128-129`） |
| GitHub repository secrets（`TELEGRAM_BOT_TOKEN`／`TELEGRAM_CHAT_ID`） | 不在 VM | GitHub（與 VM 上的同一把 Telegram token） |

持有者都是 Will 一人：主機 root 與 Tailscale SSH 只有他，雲端後台（GitHub、Cloudflare、Tailscale、OCI、Bitfinex）也只有他的帳號。

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
   sudo bash -euo pipefail <<'SH'
   f=/opt/bfx/runtime/secrets/bot/BFX_VAULT_KEK
   V=$(openssl rand -base64 32)
   [ "${#V}" -eq 44 ] || { echo "STOP: unexpected KEK length; $f untouched" >&2; exit 1; }
   printf '%s' "$V" > "$f"     # `>` 截斷後寫回同一個 inode，owner 與 mode 不變
   stat -c '%U:%g %a %s %n' "$f"                                                   # root:4600 640 44
   SH
   # 容器看到的必須是新檔（比對雜湊，不印值）；兩行要相同，不同就停下並照下面的方式放回舊 KEK
   sudo sha256sum /opt/bfx/runtime/secrets/bot/BFX_VAULT_KEK | cut -d' ' -f1
   sudo docker exec bfx-bot /app/.venv/bin/python -c \
     'import hashlib;print(hashlib.sha256(open("/run/secrets/BFX_VAULT_KEK","rb").read()).hexdigest())'
   ```

   從這一刻到第 4 步 `applied`，bot 不能重啟：重啟後它會用新 KEK 解舊憑證，解不開，於是拒絕開機（`boot_refused`，
   credential vault cannot be opened）。真的發生時：
   - 把舊 KEK 原地寫回去（重導向在 root shell 裡做，`ubuntu` 讀不到 `/root`）：
     `sudo sh -c 'cat /root/kek-rotation/BFX_VAULT_KEK > /opt/bfx/runtime/secrets/bot/BFX_VAULT_KEK'`；
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
- `ghcr.env`（GHCR PAT）：9i 的 provenance 驗證不需要 GitHub token，但 package 是 private 時 bfx-deploy 的 pull
  與讀 manifest 都要用它；要刪檔並撤銷 PAT，先把 GHCR package 改成 public（見 deploy「Env 檔規則」）。
  要換，就到 GitHub 重發 `read:packages` 的 classic PAT，照 Telegram 一節的 `read -rs` 寫法讀進 `V`，再
  `setkey /opt/bfx/runtime/ghcr.env GHCR_TOKEN`。下一輪 bfx-deploy 就會用新值。
- mirror 的 deploy key：9h 起 mirror 改用匿名 HTTPS，deploy key 應該已刪除（[deploy「Mirror」](deploy.md#mirrorgit-fetch-走匿名-https9h)）。
