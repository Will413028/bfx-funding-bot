# Operations runbook（交易狀態、包絡、停機、告警）

日常操作。部署見 [deploy runbook](deploy.md)；備份與還原見 [offsite DR](offsite-dr.md)；
行為的權威描述在 `backend/ARCHITECTURE.md` §6（offer envelope、Trading state、受管與外來 offer、
UNKNOWN 與金額指紋、自動保護、Kill switch）。決策來源：ADR 2026-09-25
`lending-envelope-replaces-probation-and-account-halt`（D1–D5、D3a）。

## 1. 三個控制層

| 層 | 控制 | 效果 |
|---|---|---|
| 每筆單 | CapitalPolicy 的包絡（§2） | 單筆上限、天期、open offer 數、利率下限；超出就不送 |
| 每幣別 | policy `enabled`（§2，UI 切換） | `false`：不掛新單，撤掉該幣別的受管 offer；已成交借款照常到期 |
| 帳戶 | `trading_state` `ACTIVE`／`HALTED` | `HALTED`：所有幣別不掛新單，planner 撤掉受管 offer |

`trading_state` 的 cause：`operator`（人）或 `auto`（自動保護，§4）。讀不到狀態或從未記錄任何決策
＝ HALTED（fail-closed）。`HALTED/operator` 只有 operator 能結束；`HALTED/auto` 在條件消失後自動恢復（§4），
超過次數上限才要人（DB trigger 與程式碼雙重把關）。
部署永遠不改變交易狀態，也不需要任何核准。

## 2. 幣別啟停與包絡設定

所有每幣別設定都是 DB 裡有版本的 CapitalPolicy，每次改動產生一個新 revision（舊的保留）。

### 啟用／停用幣別：UI（TOTP，主要路徑）

Overview「交易狀態」面板的「幣別」區塊列出每個有 applied policy 的幣別：啟用與否、包絡
（未設定時明白標示）、最近的請求與結果。停用／啟用需要填原因，經 frontend BFF 的 MFA 閘門，
webapi 只把請求排入 `capital_policy_requests`（回 202），由 bot 的 `CapitalPolicyRequestWorker`
在 account lock 下重新驗證 operator 後，走與 script 相同的 amendment 路徑寫一個新 revision
（`source` 記 `request_id`／`requested_by`／`reason`），結果顯示在該幣別下。

- 停用：不掛新單，下一個 reconcile tick 撤掉該幣別的受管 offer；手動掛的單與已成交借款不動。
  不是停機，不寫 `trading_state`，恢復也不需要 resume。停用除了 operator 驗證之外不受任何條件
  阻擋（不看 build、trading state、包絡）。
- 啟用：沒有包絡的幣別也可以啟用（結果會註明 `envelope unset`），但 offer-envelope guard
  會拒絕每一筆單直到包絡設好。fUSD 不能啟用（`unsupported_enabled_symbol`）。
- 已經是要求的狀態時結果是 `unchanged`，不寫新 revision。
- 啟用與停用各有自己的等待格（同一幣別同一動作只能有一筆 pending，409 `request_pending`），
  依送出順序套用，後送的為準。kill 另有自己的佇列、不會排在它們後面；kill 套用時會把等待中的
  **啟用**標成 `superseded_by_kill`（等待中的停用照常套用）。
- bot 的 runtime role 只能以這種方式改 `enabled`：DB trigger 拒絕 runtime role 寫入除了
  `enabled` 以外有任何不同、或不是在套用一筆仍在等待且 operator 仍有權限的同方向請求的 revision，也拒絕把 head 移到下一個 revision 以外的地方。

### 包絡與其他欄位：`amend_capital_policy`

包絡、`max_offer_amount` 等其他欄位只能用 script 改（`--enabled` 仍可用，作為 UI 不可用時的
備用）：dry run → 看報告 → 用 digest apply。在 VM 上以已部署的 backend image 跑一次性
container，用 `/opt/bfx/runtime/migrate.env`（owner role）：

```bash
python -m scripts.amend_capital_policy --exchange-account-id UUID --environment prod \
  --symbol fUST --max-offer-amount 200 --min-period-days 2 --max-period-days 2 \
  --max-open-offers 6 --rate-floor-ratio 0.5 --min-rate-apr 0.01
# 看完整報告後，同一組參數加 --apply-digest DIGEST 再跑一次
```

- 第一次設定包絡要五個欄位齊全（`envelope_incomplete` 會列出缺哪個）；之後可以只改其中一個。
- 利率下限 ＝ max(`min_rate_apr`/365, 即時 bid 中位數 × `rate_floor_ratio`)。低於下限時該幣別閒置，
  不是錯誤。
- 暫停／恢復一個幣別平常用上面的 UI；script 的 `--enabled false`／`--enabled true` 是備用。
  這是日常的停止方式，不需要 HALTED。
- 沒有包絡的幣別（舊 schema 的 policy）一律不送單（`envelope_unset`）。

## 3. 帳戶停止與恢復

### 主要：UI（TOTP）

Overview 的「交易狀態」面板。請求都經 frontend BFF 的 MFA 閘門，webapi 只把請求排入
`trading_control_requests`（回 202），由 bot 的 `TradingControlWorker` 在 account lock 下重新驗證
operator 後套用，結果顯示在面板上。

| 按鈕 | 效果 |
|---|---|
| 恢復交易 | `HALTED` → `ACTIVE`（不論是誰停的）。不帶任何限額期：每筆單本來就在包絡內 |
| Kill switch…（輸入 `KILL`） | `HALTED/operator` ＋ 每個幣別的 venue cancel-all，**連你在 Bitfinex 網頁手動掛的單也會撤**；已成交借款不受影響。等待中的其他請求標成 `superseded_by_kill`。kill 有自己的佇列、優先處理。已經 HALTED 時再按一次 ＝ 重試 cancel-all |

面板的「本次停機的 venue 撤單」顯示 `funding_cancel_all_audit` 的結果；不完整時重試 kill 或到
Bitfinex 手動撤單。**恢復只有 UI 這一條路**；static token 不能恢復。

### 緊急備用：static admin token（只能降低曝險）

UI 不可用時使用。bot 的 healthz server（container 內 port 8080，未對 host 公開）提供
`POST /admin/halt?reason=...&actor=...`，與 UI kill 同一條路徑。200＝HALTED 且每個幣別 cancel-all
都 acknowledged；502＝HALTED 已生效但 venue 部分未完成，再呼叫一次即重試。`/admin/pause` 已移除。

在 VM 上執行（token 從 container 自己的環境讀，不印出）：

```bash
sudo docker exec bfx-bot /app/.venv/bin/python -c '
import os, sys, urllib.error, urllib.parse, urllib.request
q = urllib.parse.urlencode({"reason": sys.argv[1], "actor": "operator-cli"})
r = urllib.request.Request("http://127.0.0.1:8080/admin/halt?" + q, method="POST",
    headers={"Authorization": "Bearer " + os.environ["BFX_ADMIN_TOKEN"]})
try:
    print(urllib.request.urlopen(r, timeout=120).read().decode())
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode()); sys.exit(1)
' "reason text"
```

bot 自己停著時不可用：到 Bitfinex 網頁撤單並記錄。不依賴資料庫的 break-glass 是停掉 bot container
（見 §7）：停下的 bot 不會掛新單，但也不會撤單。

## 4. 異常的四個等級

| 等級 | 情況 | 反應 | 需要你做什麼 |
|---|---|---|---|
| 1 | 違反包絡、book 過期、auth DOWN、heartbeat 過期 | 擋那一筆 | 無 |
| 2 | 送單結果不明（UNKNOWN）、讀取失敗、未被接受的 snapshot | 只隔離該幣別；settle 窗口（120s）後以金額指紋＋完整 history 自動結案 | 30 分鐘仍未結案時收到 `unknown_quarantine_aged`（之後每 6 小時），見 §5 |
| 3 | `unclassifiable_commitment`、`offer_amount_mismatch`、`identity_conflict`、`venue_lent_above_ledger`、`command_rate_exceeded` | `HALTED/auto`，之後 planner 每一輪按 id 撤**受管** offer 直到沒有（外來 offer 不碰）；停滿 15 分鐘且連續 3 個乾淨 snapshot 後自動恢復（`ACTIVE/auto`），24 小時內最多 2 次 | 收到 `auto_resume_limit_reached` 或停機一直沒解除時：查清原因後 UI 恢復 |
| 4 | 外來 offer（`foreign_exposure`）、外來 offer 成交造成的借出（`foreign_lending`）、NAV 下降（`nav_drop`） | 只告警 | 確認是不是你自己的操作 |

- **不會**觸發任何反應：借款到期造成 lent 減少、reconcile 補回 WS 漏掉的成交或撤單。
- writer lock 遺失不是交易決策：bot 直接退出、container 重啟、開機時重新等 lock（Telegram 收到
  `daemon_fatal`）。
- 拒絕開機（schema 與 build 不符、policy 讀不到）：只發 `venue_offers_may_remain` 然後退出，
  不寫交易狀態、不碰 venue；修好後的下一版會照常開機。venue 上已有的受管 offer 仍在包絡內；要撤就用 kill。
- 第 3 級自動恢復：衝突類（`offer_amount_mismatch`、`identity_conflict`、`unclassifiable_commitment`）條件還在就一直
  停著；`venue_lent_above_ledger` 與 `command_rate_exceeded` 停機後偵測不到，15 分鐘內沒再發生就恢復，
  一天第 3 次停機才要人。恢復時 Telegram 會收到 `trading state HALTED -> ACTIVE`（cause `auto`，actor `auto-resume`，
  reason 列出停機原因、停了幾分鐘與 3 個乾淨 snapshot 的 event_seq）。在 HALTED/auto 期間按 kill 會改寫成
  `HALTED/operator`，之後不會自動恢復。log：`auto_resumed`、`auto_resume_refused`。
- 第 3 級處理步驟（自動恢復沒發生、或收到 `auto_resume_limit_reached`）：
  1. 看 Telegram 與 UI 的 cause/reason；container log 用 `docker logs bfx-bot --since 1h`。
  2. 看 log 的 `managed_offer_sweep`（requested／failed）；撤不掉的受管 offer 每一輪會重試，急的話用 kill。
  3. 查清 ledger 與 venue 的差異（`/admin/trading-status`、`/admin/dry-evaluate`）。
  4. 原因消除後在 UI「恢復交易」。

## 5. UNKNOWN 的人工處理

絕大多數 UNKNOWN 在 settle 窗口後自動結案：找到帶同一指紋、同條件的 offer 就認領；history 完整
涵蓋送單時間卻沒有，就判定未送出。以下情況會維持隔離、等你處理：history 不完整、兩個以上候選、
候選已被其他紀錄認領、或指紋相同但其他條件不符。

Overview 的 uncertainty 明細提供請求（同樣經 MFA、排入 `uncertainty_resolution_requests`，由 bot 套用；
需要新鮮的 reconcile fence，過期會被拒 `stale_reconcile_fence`）：

| uncertainty kind | 可用動作 |
|---|---|
| `submit_outcome_unknown`（UNKNOWN 送單） | `bind-to-venue`：綁到 venue 上唯一且完全符合的 offer；`mark-not-accepted`：有完整 venue history 證明 venue 沒收下 |
| `unattributed_venue_offer`、`unsupported_venue_exposure`（2026-09-25 前留下的舊紀錄） | `manual-resolution`：`closed_at_venue` 或 `accepted_external_exposure`。新的外來 offer 不再產生這種紀錄 |

規則：

- 零個或多個候選 ＝ 維持 UNKNOWN，不要猜。不要刪資料列、不要自己合成 venue reference。
- 需要 DB 還原或 venue 寫入之後的回滾判斷時，改走
  [rollback after a venue write](rollback-after-venue-write.md)。

## 6. 告警

兩條 Telegram 路徑，**用同一組 bot token 與 chat id**：

| 來源 | 設定檔 | 內容 |
|---|---|---|
| bot 內的 sink（`observability/alerts.py`，非阻塞） | `/opt/bfx/runtime/bot.env` 的 `TELEGRAM_BOT_TOKEN`／`TELEGRAM_CHAT_ID` | 交易狀態轉換、自動保護、kill 結果、恢復結果、幣別啟停結果（`capital_policy_request_*`）、`foreign_exposure`、`foreign_lending`、`unknown_quarantine_aged`、`nav_drop`、`venue_offers_may_remain` |
| host 工具 `bfx-notify` | `/opt/bfx/runtime/notify.env`（同樣兩個 key） | bfx-deploy 結果、bfx-backup-check（RPO>300s、evidence 超過 15 分鐘、restore heartbeat 超過 35 天）、`bfx-alert@` 的 unit 失敗（pgBackRest 備份、restore test） |

- 兩個都空 → bot 只記 log（開機時會說一次）；`bfx-notify` 永遠 exit 0，送不出去只記 journal。
- **換 token 或 chat id 要兩個檔都改**：改 `notify.env` 立即生效；改 `bot.env` 後
  `sudo bfx-deploy --recreate`（§7）。
- **為什麼是兩條、兩份檔**（design review #14，刻意保留）：兩條路徑在容器／主機的邊界兩側，
  各自要在對方掛掉時還能發出告警——bot 死掉或起不來時由主機的 bfx-deploy／`bfx-alert@` 通報，
  主機 timer 停擺時交易事件仍由 bot 自己通報；合成一條就會讓其中一邊失去告警。token 分兩份檔是
  最小權限：`bot.env` 只給 bot container，`notify.env` 只給 root 的主機工具，DR unit（`ubuntu`）
  兩份都讀不到。
- 測試：`sudo bfx-notify --level info "test"`。
- Grafana 仍是第二條觀測路徑。

## 7. 改了 runtime env 之後重建 container

bfx-deploy 只在有新 release（或 `--retry`）時自動重建 container；只改 `/opt/bfx/runtime/*.env`
不會觸發。改完後：

```bash
sudo bfx-deploy --recreate --dry-run   # 預覽：同一個 release
sudo bfx-deploy --recreate
```

它和一般部署走同一套：env 檢查、ledger `started` 列、`--force-recreate`、身分檢查、健康等待、
結束列與 Telegram。沒有 rollback：不健康就停 bot 並記 `failed`，修好 env 再跑一次。細節見
[deploy runbook](deploy.md#--recreate改了-runtime-env-之後)。

## 8. 定期與背景工作

| unit | 排程 | 作用 |
|---|---|---|
| `bfx-deploy.timer` | 每 5 分鐘（`*:2/5`） | 部署最新綠燈 main |
| `bfx-pgbackrest-backup.timer` / `bfx-pgbackrest-status.timer` | 見 unit | 備份與 RPO evidence（`OnFailure=bfx-alert@`） |
| `bfx-backup-check.timer` | 每 5 分鐘（`*:1/5`） | evidence 與 restore heartbeat 檢查，連續兩次才告警，6 小時重發 |
| `bfx-restore-test.timer` | 每月 1 日 09:17 UTC | `bfx-restore-test@current`：用已部署 release 的 DR 腳本做 isolated restore ＋ prefix-hash 驗證（[offsite DR](offsite-dr.md#monthly-and-change-triggered-prefix-restore-test)） |
| `bfx-weekly-report.timer` | 每週一 04:17 UTC | 每週量測鏈：attribution／realized interest／G3 報告＋研究重驗（見下方「每週報告」；`OnFailure=bfx-alert@`） |
| `bfx-halt-watch.timer` | 見 unit | 以 `/admin/dry-evaluate` 檢查停機是否真的生效 |

`systemctl list-timers 'bfx-*'` 看實際啟用狀態。

### 每週報告（`bfx-weekly-report`）

步驟定義在 `deploy/vm/ops/docker-compose.weekly-report.yml`，和主機工具一起由 bfx-deploy 在每次
成功部署後安裝到 `/usr/local/lib/bfx-ops/releases/<rev>/ops`（`current` 指向它）；
`bfx-weekly-report.service` 在 `managed-units` 裡，同時被安裝與 `daemon-reload`。
VM 上 `/home/ubuntu/bfx-funding-bot` 的 working tree **不再被讀取**（它不會被更新）。

- **Image**：ledger 最新 `deployed` 列的 backend digest（`<repository>@sha256:…`，與 production
  跑的是同一個），不用 `bfx-bot:local`。步驟來自已安裝的工具版本；若兩者 revision 不同
  （部署後工具安裝失敗），照跑並在 journal 印 warning。
- **Env**：只傳三個值——`DATABASE_URL`、`BFX_EXCHANGE_ACCOUNT_ID` 取自 `/opt/bfx/runtime/bot.env`
  （bot 的 restricted role），`BFX_DEPLOYMENT_ENV` 取自該部署 revision 的 `live.env`
  （`/var/lib/bfx-deploy/releases/<rev>/live.env`）。`bot.env` 其餘的 secret 不會進 container。
- **Hardening**：read-only rootfs、UID 1000、cap-drop ALL、no-new-privileges，只有 `/tmp`（tmpfs）
  與 `/home/ubuntu/bfx/reports` → `/reports` 可寫；CI 以
  `compose_policy.py --kind weekly-report` 檢查。reports 目錄必須已存在且 uid 1000 可寫
  （`sudo chown 1000:1001 ~/bfx/reports && chmod 775`），不會自動建立：目錄不存在時 runner 以
  `reports_dir_missing` 拒跑（Compose 2.x 與 v5 對未設定的 `create_host_path` 解讀相反，不交給 Docker 決定）。
- Timer 不在 `managed-units`：安裝 unit 檔會覆蓋 `systemctl mask`，排程開不開由 operator 決定。
- **G3 對帳 FLAG 的確認**：最近 8 個已結算週內有 FLAG，G3 判 UNRELIABLE。查清原因後若要放行，在
  `docker-compose.weekly-report.yml` 的 `run_g3_live_validation` 那行加 `--ack-week YYYY-MM-DD="理由"`
  （週一日期、理由必填），走 PR 合併、隨部署生效；報告的 md／JSON 會列出每個確認與它是否用上。
  超出 8 週窗的 FLAG 不再擋判定，確認可以拿掉。

手動執行（VM，root）：

```bash
sudo /usr/local/lib/bfx-ops/current/ops/.venv/bin/python \
  /usr/local/lib/bfx-ops/current/ops/bfx_weekly_report.py --dry-run   # 印出 image、revision、argv；不執行
sudo systemctl start --no-block bfx-weekly-report.service            # 真的跑一次（最長 90 分鐘）
journalctl -u bfx-weekly-report.service -f
ls -lt /home/ubuntu/bfx/reports | head
```

排程的啟停：

```bash
sudo systemctl unmask bfx-weekly-report.timer
sudo systemctl enable --now bfx-weekly-report.timer
sudo systemctl disable --now bfx-weekly-report.timer   # 暫停
```

## 9. 同一把 API key 的其他使用者（nonce 衝突）

Bitfinex 對**每把 API key** 只接受遞增的 nonce，並以**抵達順序**判斷：比最後收到的還小就回
HTTP 500（body 為 `nonce: small`）。bot 內所有簽章請求共用一個 `AuthRequestGate`
（`external/bitfinex/nonce.py`），依序送出，下單／撤單／kill 優先於背景讀取；但這個 gate
**只管 bot 這一個 process**。以下用同一把 key 的呼叫不經過它，可能讓 bot 的請求（包括撤單或
kill 的 cancel-all）被拒：

- webapi 的 `verify_account_key`（UI 驗證／更新帳戶 key 時呼叫 `auth/r/permissions`）
- 一次性腳本：`scripts/bootstrap_capital.py`、`bootstrap_account_credential.py`、
  `cutover_identity.py`、`cutover_projection.py` 等會打 Bitfinex auth 端點的工具
- 任何在其他機器上用同一把 key 的手動測試

做法：

- **交易進行中不要跑這些**；需要時先停止帳戶（§3）或在沒有下單活動時執行，跑完看 bot log 有無
  `bitfinex_auth_http_error ... nonce`。
- 需要常態的外部讀取（驗證、報表、除錯）時，改用**另一把只有讀權限的 key**：nonce 是按 key
  計算，不同 key 互不影響，唯讀也避免誤下單。
- bot log 出現 `bitfinex_auth_gate_slow_wait`（等 gate 超過 2 秒，含等待者種類）表示 venue 變慢或
  讀取堆積；背景讀取每筆最多 10 秒就會被切斷。
