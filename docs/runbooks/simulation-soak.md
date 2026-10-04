# 模擬 soak（simulated venue，S1-7 的入場證據）

在 VM 上以**一次性 container**（`docker run --name bfx-sim`）跑一個 simulated-venue bot，吃公開行情、
寫進自己的資料庫 `bfx_sim`，72 小時以上，用 `scripts/sim_soak_report.py` 對照 ADR D3 的門檻。
門檻、活動下限、故障注入與 kill 流程的決定見
[ADR 2026-10-03](../adr/2026-10-03-simulation-runs-the-ledger-on-a-simulated-venue.md)
（D3 與 2026-10-04 修訂）。這份文件是執行步驟；數字以 ADR 與 `sim_soak_report.py` 的常數為準。

- **執行者**：orchestrator（agent）以 root 在 VM 上執行每一步。Will 只決定門檻與 `DROP DATABASE`。
- **secret 不印出**：env 檔只用「檔案 → 檔案」或「檔案 → container」寫入，不 `cat`、不 `echo`、
  不貼進 chat 或 log。
- **sim 與 live 共用同一個 IP 的公開 rate limit**（REST 與 WS）。sim 不得讓 live bot 吃到 429（abort rule，§6）。
- **sim 不在 compose 也不在 bfx-deploy 管轄內**：`compose_policy` 只檢查渲染出來的 compose 檔，
  bfx-deploy 只拒絕佔用三個正式名稱的外來 container。`bfx-sim` 不會被它們看見，所以部署後的重啟是
  orchestrator 的責任（§7）。
- **網路別名**：`bfx-sim`。**絕不**用 `bfx-bot`（webapi 以 `bfx-bot:8080` 解析 live bot）。
- **用來驗證的範圍**：ledger、組裝與 runtime。simulated venue 不驗證 Bitfinex 本身的語意，成交模型偏樂觀，
  模擬 P&L 不是決策依據（ADR D2）。

## 0. 名詞

| 名稱 | 內容 |
|---|---|
| `<owner>` | Postgres owner role（`AGENTS.local.md`；`fresh-host-setup.md` §1） |
| `$MIRROR` | VM 上 git mirror 的路徑（`AGENTS.local.md`） |
| `$IMAGE` / `$REV` | 正在跑的 `bfx-bot` 的 image（`repo@sha256:…`）與它的 revision label |
| `$SIM_ACCOUNT` | 這次 soak 的新 account UUID（每次 soak 重新產生，不重用） |
| generation | 一個 `bfx-sim` container 的一生。每次部署後重啟或刻意重啟都換一代 |
| `soak-ledger.jsonl` | orchestrator 的流水帳（`/home/ubuntu/bfx/reports/sim-soak/`）：時間、事件、舊／新 digest、migrate 結果、kill／resume |

報告與 archive 放 `/home/ubuntu/bfx/reports/sim-soak/`（不進 repo）：每代一份 `docker logs` 與一份 `/metrics` 原文。

## 1. T-1 天：前置

1. 本 PR 已合併並部署到 live bot（新增 `bfx_venue_rest_rate_limited_total`，abort rule 靠它）。
   `systemctl status bfx-deploy.service` 為 inactive，live bot 健康。
2. `docker info --format '{{.LoggingDriver}}'` 是 `json-file`（§5 的 `--log-opt` 才生效）。
3. **24 小時 live-bot 429 基準**（部署本 PR 之後起算，sim 啟動前取得）。每 15 分鐘取一次並記進 `soak-ledger.jsonl`：

   ```bash
   docker exec bfx-bot /app/.venv/bin/python -c '
   import urllib.request
   body = urllib.request.urlopen("http://127.0.0.1:8080/metrics", timeout=10).read().decode()
   print("\n".join(l for l in body.splitlines() if l.startswith("bfx_venue_rest_rate_limited_total")))'
   docker logs bfx-bot --since 24h 2>&1 | grep -c 'rate limited\|BitfinexRateLimited'
   docker logs bfx-bot --since 1h 2>&1 | grep -c funding_book_rest_reconcile_failed   # 取 24 個 1 小時窗的最大值
   ```

   基準預期為 0。基準不是 0 時先查原因，不要開跑。
4. 唯讀取得 prod 目前套用的 fUST policy 與 funding wallet 金額（sim 照抄，guard chain 的行為才一致）。

## 2. 建立 `bfx_sim`（owner，一次）

與 [fresh-host-setup.md](fresh-host-setup.md) §1a 同樣的 role 與 default privileges，只是對象是新 database
（default privileges 以 database 為單位）：

```bash
docker exec -i bfx-postgres psql -U <owner> -d bfx -v ON_ERROR_STOP=1 <<'SQL'
CREATE DATABASE bfx_sim OWNER <owner>;
REVOKE CONNECT ON DATABASE bfx_sim FROM PUBLIC;   -- webapi／webauth 不連
GRANT CONNECT ON DATABASE bfx_sim TO bfx_bot;
SQL
docker exec -i bfx-postgres psql -U <owner> -d bfx_sim -v ON_ERROR_STOP=1 <<'SQL'
GRANT USAGE ON SCHEMA public TO bfx_bot;
ALTER DEFAULT PRIVILEGES FOR ROLE <owner> IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bfx_bot;
ALTER DEFAULT PRIVILEGES FOR ROLE <owner> IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO bfx_bot;
SQL
```

pgBackRest 備份整個 cluster，`bfx_sim` 存在期間也在備份與還原演練之內（stanza 以名稱檢查，不以 database）。

## 3. Secret env 檔（root 寫入，不印出）

```bash
umask 077
SIM_ACCOUNT=$(cat /proc/sys/kernel/random/uuid)

# owner：migrate 與 bootstrap 用，URL 指到 bfx_sim
sed -n 's#^\(DATABASE_URL=[^ ]*/\)bfx$#\1bfx_sim#p' /opt/bfx/runtime/migrate.env > /opt/bfx/runtime/sim-migrate.env
grep -c '^DATABASE_URL=.*/bfx_sim$' /opt/bfx/runtime/sim-migrate.env          # 必須印 1

# bfx_bot：sim container 與報告用。sim 專用 admin token，全新 account
{ sed -n 's#^\(DATABASE_URL=postgresql://bfx_bot:[^ ]*/\)bfx$#\1bfx_sim#p' /opt/bfx/runtime/bot.env
  printf 'BFX_ADMIN_TOKEN=%s\n' "$(openssl rand -hex 32)"
  printf 'BFX_EXCHANGE_ACCOUNT_ID=%s\n' "$SIM_ACCOUNT"
  printf 'BFX_SIM_INITIAL_WALLETS=UST:%s\n' "<prod funding wallet 的整數金額>"
  printf 'BFX_SIM_FAULTS=%s\n' "unknown_5xx=0.01,unknown_placed_lost=0.005,history_error=0.005,seed=1"
} > /opt/bfx/runtime/sim.env
grep -c '^DATABASE_URL=postgresql://bfx_bot:.*/bfx_sim$' /opt/bfx/runtime/sim.env   # 必須印 1
```

- 兩個 `grep -c` 不是 1（例如 URL 帶 query string 使 `sed` 沒有輸出）就停下，不要繼續。
- `sim.env` **不得**含 `BFX_VAULT_KEK`（simulated 開機會拒絕）、`TELEGRAM_*`（sim 的告警只進 log，由 orchestrator
  每 15 分鐘檢查、每日報告；`[shadow]` 噪音不進 prod 頻道）、`BFX_OPERATOR_USER_ID`、`BFX_DEPLOYMENT_ID`
  （後者是 prod ledger 的 row 名）。
- `BFX_SIM_INITIAL_WALLETS` 只在 venue log 為空時生效（第一代）；之後各代不會再注資。
- **`BFX_SIM_FAULTS`**（決定 A）：整個 72 小時都開，種子化、每個請求獨立抽籤。上面是起始值；rate 與 seed 在開跑前定案。
  每一代換新的 seed（第 N 代用 `seed=N`），否則重啟後的請求序號從 1 重數，會重演同一串抽籤。
  只作用在 simulated venue 行程內的 transport（submit 與 history），不會到 Bitfinex；Bitfinex 組裝見到這個變數會拒絕開機。
  每一次注入都以 `fault_injected` 事件寫進 `sim_venue_event`（跨重啟存在），報告只靠 DB 就分得出注入與自然發生。
  `history_error` 的比例會直接壓低 accepted cycle 比例（每個 cycle 的 history 讀取數乘上 rate）；
  開跑後第一個小時跑一次報告，`d3.accepted_cycle_ratio` 的趨勢低於 99% 時降低該 rate 並**重新計 72 小時**。
- 兩個 env 檔和 `shadow.env` 的 key 互不重疊（`docker run` 對重複 key 的優先序未驗證，因此刻意避開）。

## 4. Image、migrate、蓋章、bootstrap

### 4.1 Image 與 `shadow.env`（取自正在跑的 release）

```bash
IMAGE=$(docker inspect bfx-bot --format '{{.Config.Image}}')               # repo@sha256:…
REV=$(docker image inspect "$IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
mkdir -p /var/lib/bfx-sim/$REV
git -C "$MIRROR" show "$REV:deploy/vm/shadow.env" > /var/lib/bfx-sim/$REV/shadow.env
```

`$REV` 為空就停下：沒有 `BFX_SOURCE_REVISION`，報告的重啟次數不可信（報告會判 FAIL）。
`shadow.env` 與 `live.env` 的差異由 `backend/tests/scripts/test_shadow_profile.py` 釘住。

### 4.2 Migrate 到 head（用**這一代的** image）

```bash
docker run --rm --name bfx-sim-migrate --pull=never --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m --user 1000:1000 --cap-drop=ALL \
  --security-opt=no-new-privileges --network bfx_default --workdir /app --entrypoint "" \
  --env-file /opt/bfx/runtime/sim-migrate.env --env PYTHONDONTWRITEBYTECODE=1 \
  "$IMAGE" /app/.venv/bin/alembic upgrade head
```

以同樣的 flags 加上 `… alembic current` 與 `… alembic heads` 比對，兩者必須相同。

### 4.3 蓋 realm `shadow`（owner，一次）

```bash
docker exec -i bfx-postgres psql -U <owner> -d bfx_sim -v ON_ERROR_STOP=1 -c \
  "INSERT INTO database_realm (realm, stamped_at_ms, actor)
   VALUES ('shadow', (extract(epoch FROM clock_timestamp())*1000)::bigint, 'owner bootstrap');"
```

### 4.4 Bootstrap（owner；冪等）

```bash
docker run --rm --name bfx-sim-bootstrap --pull=never --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m --user 1000:1000 --cap-drop=ALL \
  --security-opt=no-new-privileges --network bfx_default --workdir /app \
  --env-file /opt/bfx/runtime/sim-migrate.env --env PYTHONDONTWRITEBYTECODE=1 \
  --entrypoint /app/.venv/bin/python "$IMAGE" \
  -m scripts.bootstrap_simulation_db --exchange-account-id "$SIM_ACCOUNT" \
  --symbol fUST --disabled-symbol fUSD --max-offer-amount <prod> --min-period-days <prod> \
  --max-period-days <prod> --max-open-offers <prod> --rate-floor-ratio <prod> --min-rate-apr <prod> \
  --activate
```

輸出的 `trading_state` 必須是 `activated`。**沒有 `--activate`，新 scope 沒有任何 `trading_state` 列，等於 HALTED：
bot 一筆都不送，而 D3 的每一條安全門檻對一個閒置的 bot 都成立**（這就是活動下限存在的原因）。
`--activate` 只在 scope 完全沒有狀態列時寫入 `ACTIVE`；已有任何一列（包括 HALT）都不動，
輸出會是 `kept_active`／`kept_halted`。腳本從不恢復 HALT。

## 5. 啟動

```bash
GEN=1
docker run -d --name bfx-sim --label autoheal=false --restart no \
  --network bfx_default --network-alias bfx-sim \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m --cap-drop ALL \
  --security-opt no-new-privileges:true --user 1000:1000 -w /app \
  --cpus 1.0 --cpu-shares 512 --memory 1g --memory-swap 1g --pids-limit 256 \
  --log-opt max-size=200m --log-opt max-file=5 \
  --env-file /opt/bfx/runtime/sim.env --env-file /var/lib/bfx-sim/$REV/shadow.env \
  -e BFX_IMAGE_DIGEST="${IMAGE#*@}" -e BFX_SOURCE_REVISION="$REV" \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH= -e PYTHONHOME= -e PYTHONUSERBASE= \
  -e LD_PRELOAD= -e LD_LIBRARY_PATH= -e LD_AUDIT= \
  --entrypoint /app/.venv/bin/python "$IMAGE" -m bfx_funding_bot.apps.bot
```

- 1 CPU／1 GiB，CPU 權重減半：live bot 沒有資源限制，量過的 bot RSS 約 265 MiB，這個上限不會餓死它。
- `--restart no`：非預期退出是 soak 的發現，不是要掩蓋的事。`BFX_SOURCE_REVISION` 必帶（`execution_decisions.service_version`
  就是報告數重啟的依據）；不要加 `BFX_DEPLOYMENT_ID`。
- 開機檢查順序：schema head → realm 章 = `shadow` → authority 支援 → account active → 每個 symbol 有套用的 policy。
  任何一個拒絕都是 `venue_offers_may_remain` 後退出；修好再開，不要繞過。
- 確認：一個 reconcile 週期（60 秒）內出現第一個 accepted observation，接著第一個 ack 的 submit。兩者都沒出現就是 vacuous，
  先查（`docker logs bfx-sim`、`trading_state`），不要讓時鐘空跑。記下起算時間到 `soak-ledger.jsonl`。
- `/metrics` 在 sim 的 8080（自己的 netns，沒有對 host 公開）。VM 的 Prometheus 設定不在 repo；不加 scrape job 時，
  以 `docker exec bfx-sim … urllib …/metrics` 取原文（做法同 §1 的 live 指令，把 container 名換掉）。

## 6. 每 15 分鐘：abort rule

檢查 live bot 與 sim。符合下列任一條就**立刻** `docker stop bfx-sim`，並先查根因再重啟（**72 小時重新計**：中斷讓 trades feed 與時鐘不連續）：

- (a) `bfx_venue_rest_rate_limited_total`（live）比 §1 的基準多出 ≥ 1，或 `grep -c 'rate limited\|BitfinexRateLimited'` 增加；
- (b) live `bfx_trading_ready` 為 0 超過 5 分鐘且原因是 book 不可用；
- (c) 任一個 1 小時窗的 `funding_book_rest_reconcile_failed` 超過 §1 基準的最大值。

同時看 sim 自己：`bfx_sim_venue_internal_failures_total`、`bfx_sim_venue_unexpected_requests_total` 必須是 0（>0 就是模擬器本身的缺陷，
立即記錄；它們是報告的 `extra.*` 門檻），`bfx_sim_venue_feed_stream_total{event="disconnected"}` 與
`bfx_sim_venue_feed_watermark_lag_seconds{symbol}`（持續成長＝trades 串流停滯）。

## 7. 視窗內的部署（每一次）

偵測：`docker inspect bfx-bot --format '{{.Config.Image}}'` 與 sim 的 image 不同（或 prod deployments ledger 有新的 `deployed` 列；
`bfx-deploy.timer` 每 5 分鐘一次，所以每 5 分鐘看一次）。偵測到後：

進程計數器隨重啟歸零，所以順序固定：先存這一代的 `/metrics`，再停、存 log、移除。存不下來的那一代，報告的 `extra.*` 就是 `UNAVAILABLE`。

```bash
R=/home/ubuntu/bfx/reports/sim-soak
docker exec bfx-sim /app/.venv/bin/python -c '
import sys, urllib.request
sys.stdout.write(urllib.request.urlopen("http://127.0.0.1:8080/metrics", timeout=10).read().decode())' \
  > $R/gen$GEN-$REV.metrics
docker stop -t 30 bfx-sim
docker logs bfx-sim > $R/gen$GEN-$REV.log 2>&1      # 在 rm 之前，否則 log 隨 container 消失
docker rm bfx-sim
```

1. 重新取 `$IMAGE`／`$REV`，把新 revision 的 `shadow.env` 放到 `/var/lib/bfx-sim/$REV/`（§4.1）。
2. **用新 image migrate `bfx_sim`**（§4.2）。舊 image 的 sim 必須先停：舊程式不得跑在新 head 上。漏了 migrate 會被開機的 schema head 檢查拒絕，不會悄悄通過。
3. `GEN=$((GEN+1))`，把 `sim.env` 裡 `BFX_SIM_FAULTS` 的 `seed=` 換成新的 `GEN`，再依 §5 啟動。
4. 確認新一代的第一個 accepted observation，append 一列到 `soak-ledger.jsonl`。
5. **停機時間盡量短**（目標 < 1 小時）。live feed 的啟動 backfill 以 venue 記錄的 `market_through` 為起點（不再是固定 1 小時，停機超過 1 小時也補得到），
   但受 `retention_ms`（72 小時）與 Bitfinex 公開 trades 的保留期限制，且每個 symbol 最多回補 10 頁 × 1000 筆；
   超過這個量時 `simulated_venue_trades_backfill_truncated` 會出現在 log，視為缺口（成交偏悲觀、不會被計數）。

同一個 digest 的重啟**不算**部署重啟（報告以 `service_version` 變動計）；視窗內自然發生的部署少於 2 次就延長視窗。

## 8. Kill 與恢復（決定 B）：測試 harness，不是產品流程

在視窗的第 24 到 48 小時之間做**一次**：

1. kill：以 §3 的 `sim.env` 內的 token，對 sim 的 healthz 送 `POST /admin/halt?reason=...&actor=soak-kill`
   （做法同 [operations.md](operations.md) §3「緊急備用」，container 換成 `bfx-sim`，`docker exec` 讓 token 留在 container 內）。
   200＝`HALTED/operator` 且每個幣別 cancel-all 都 acknowledged；502＝HALTED 已生效但 venue 部分未完成，再呼叫一次。
   報告以「cause=operator 的 HALT ＋ 有 `acknowledged` 的 `funding_cancel_all_audit`」計一次 kill。
2. 維持 HALTED 至少一個 reconcile 週期：下一個 accepted observation 的 basis 必須是 `conserved`，venue 沒有任何受管的 resting offer。
3. **刻意重啟同一個 digest**（boot-while-halted）：照 §7 的存檔順序停掉、重開（換 seed 與 `GEN`），但不 migrate、不換 image。
   確認新一代在 HALTED 下開機、有 accepted observation、**沒有**任何 submit。
4. 恢復：owner 在 `bfx_sim` 追加一列（SQL 是**測試 harness 的步驟**；產品的恢復只有 UI 的 MFA operator 路徑，
   `auto` 原因則由系統自動恢復，見 operations.md；sim 沒有 operator 身分，所以只能這樣）：

   ```sql
   -- psql -U <owner> -d bfx_sim
   INSERT INTO trading_state (exchange_account_id, deployment_environment, state, cause, actor, reason, created_at_ms)
   VALUES ('<SIM_ACCOUNT>', 'shadow', 'ACTIVE', 'operator', 'soak-orchestrator',
           'soak harness: resume after the kill test', (extract(epoch FROM clock_timestamp())*1000)::bigint);
   ```

   trigger 允許 `operator` 從 `HALTED` 轉 `ACTIVE`。daemon 每次決策與每個 reconcile tick 都重讀 `trading_state`，不需重啟。
5. 恢復之後至少再交易 **24 小時**，且其間有 ack 的 submit（報告的 `amendment.trading_after_resume`）。
   把 kill、重啟與 resume 的時間寫進 `soak-ledger.jsonl`。

## 9. 每日報告與最終判定

報告以 sim 自己的 DB 為準，在 repo 外的 daily log 記錄；每一代的 `/metrics` 原文都要在手上：

```bash
docker run --rm --name bfx-sim-report --pull=never --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m --user 1000:1000 --cap-drop=ALL \
  --security-opt=no-new-privileges --network bfx_default --workdir /app \
  --env-file /opt/bfx/runtime/sim.env -v /home/ubuntu/bfx/reports/sim-soak:/reports:ro \
  --env PYTHONDONTWRITEBYTECODE=1 --entrypoint /app/.venv/bin/python "$IMAGE" \
  -m scripts.sim_soak_report --exchange-account-id "$SIM_ACCOUNT" \
  --since <soak 起算 UTC，如 2026-10-05T00:00:00Z> --until <現在或結束時間 UTC> \
  --metrics /reports/gen1-<rev>.metrics --metrics /reports/gen2-<rev>.metrics   # 每一代一份，含正在跑的這一代的即時取樣
```

每個 criterion 印 `PASS`／`FAIL`／`UNAVAILABLE` 與證據數字；exit code 0＝全部通過，1＝有 FAIL，3＝無 FAIL 但有讀不到的來源
（永遠不把讀不到當成 0），2＝拒絕執行（資料庫不是 `shadow` 章，或參數不可用）。

- 視窗還沒滿 72 小時、還沒有 2 次部署重啟、還沒 kill 時，對應的 criterion 本來就是 FAIL；每日報告看的是趨勢與其他項目。
- 通過條件（ADR D3 與修訂）：視窗 ≥ 72 小時；≥ 2 次部署重啟（新 revision，且沒有 `unidentified`）；≥ 1 次 kill ＋ 恢復後 ≥ 24 小時交易；
  `unexplained_lending` ＝ 0（外加 `foreign_lending` ＝ 0）；非注入的 quarantine／UNKNOWN ＝ 0；accepted cycle ≥ 99%；
  注入的 UNKNOWN 全數自動結案（且至少有一筆）；模擬器內部失敗與 unexpected request ＝ 0；
  活動下限：≥ 50 筆 ack 的 submit、≥ 10 次成交、≥ 10 次撤單或 reprice、≥ 1 筆因到期結清的 credit、視窗內每個完整 UTC 日 ≥ 1 筆利息。
- 每日報告另列（報告腳本外）：已過小時、各代的 revision、sim 與 live 的 RSS／CPU、live-bot 429 與 ready 狀態、異常。
- 事件數會隨時間成長（約每個已驗證請求一筆 `NonceAdvanced`）：報告尾端的 `sim_venue_event` 列數接近 200 000，
  或 boot replay 超過 5 秒，就是 `modules/simulated_venue/__init__.py` 寫的 snapshot 觸發條件。

## 10. 結束與拆除

視窗滿足 §9 的條件後：

1. 照 §7 的順序存最後一代的 `/metrics`、`docker stop`、存 log、`docker rm bfx-sim`。
2. 跑最終報告（§9），把 PASS／FAIL 與證據數字記進 plan 的 Progress，作為 S1-7 入場證據。
3. `rm /opt/bfx/runtime/sim.env /opt/bfx/runtime/sim-migrate.env`。
4. `bfx_sim` 先保留到報告被接受；`DROP DATABASE bfx_sim` 是破壞性動作，**先問 Will**（也可以留給 S1-7 之後的回歸 soak）。
