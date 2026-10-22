# Operations runbook（交易狀態、包絡、停機、告警）

日常操作。部署見 [deploy runbook](deploy.md)；備份與還原見 [offsite DR](offsite-dr.md)；
行為的權威描述在 `backend/ARCHITECTURE.md` §6（offer envelope、Trading state、受管與外來 offer、
UNKNOWN 與金額指紋、自動保護、Kill switch）。決策來源：ADR 2026-09-25
`lending-envelope-replaces-probation-and-account-halt`（D1–D5、D3a）。

## 1. 三個控制層

| 層 | 控制 | 效果 |
|---|---|---|
| 每筆單 | CapitalPolicy 的包絡（§2） | 單筆上限、天期、open offer 數、利率下限；超出就不送 |
| 每幣別 | policy `enabled`（§2：啟用只走 CLI，停用 CLI 或 UI） | `false`：不掛新單，撤掉該幣別的受管 offer；已成交借款照常到期 |
| 帳戶 | `trading_state` `ACTIVE`／`HALTED` | `HALTED`：所有幣別不掛新單，planner 撤掉受管 offer |

`trading_state` 的 cause：`operator`（人）或 `auto`（自動保護，§4）。讀不到狀態或從未記錄任何決策
＝ HALTED（fail-closed）。`HALTED/operator` 只有 operator 能結束；`HALTED/auto` 在條件消失後自動恢復（§4），
超過次數上限才要人（DB trigger 與程式碼雙重把關）。
部署永遠不改變交易狀態，也不需要任何核准。

**資金權威（`capital_authority_epoch`）**：只增不改的表，最新一列（`epoch_seq` 最大）決定資金事實由誰負責。
現行唯一支援的值是 `ledger`：最新 epoch 不是 `ledger` 時，非 owner（`bfx_bot`）對 ledger 表的
INSERT（以及 clock／mirror 的 UPDATE）一律被 DB trigger 拒絕。`legacy` 只是 epoch 的歷史列（每個資料庫都由 `f6a7b8c9d0e1` 先種下 `legacy`，genesis migration 再追加 `ledger`）。
新資料庫由 genesis migration 取得 `ledger`；prod 由 S1-7 的切換追加，不要手動改。
bot、webapi 與 owner 腳本開機時各讀一次：表或列缺失、值不是 `ledger` 就拒絕開機；真實 venue 的 bot
另要求最新 epoch 的寫入者是切換或 genesis migration（`ledger_epoch_writer_unknown`），拒絕走
`_refuse_live_boot` 告警。

## 2. 幣別啟停與包絡設定

所有每幣別設定都是 DB 裡有版本的 CapitalPolicy，每次改動產生一個新 revision（舊的保留）。

### 啟用／停用幣別：啟用只走 CLI，停用 CLI 或 UI

```bash
sudo bfx-op enable fUST --reason "為什麼啟用"     # 只走 CLI（ADR 2026-10-15 D4）
sudo bfx-op disable fUST --reason "為什麼停用"    # CLI 或 UI 都可以
sudo bfx-op status                                # 每個幣別的啟用、包絡與最近的請求
```

CLI 的入口、身分與 exit code 見 §10。兩條路都只把請求排入 `capital_policy_requests`，由 bot 的
`CapitalPolicyRequestWorker` 在 account lock 下重新驗證 operator 後，走與 script 相同的 amendment
路徑寫一個新 revision（revision 的 `operator_request_id` 指回該請求；誰、為什麼是請求自己的欄位，
不複製進 revision 的 `source`）。CLI 預設等到結果才結束；UI 的 Overview「交易狀態」面板「幣別」
區塊列出同樣的狀態與結果（CLI 排入的請求也在內）。UI 沒有啟用按鈕，停用中的幣別只顯示 CLI 指令；
webapi 也沒有啟用的端點，資料庫的 trigger 拒絕 table owner 與 `bfx_operator` 以外的 role 排入
`enable`（migration `36e59526f8cf`）。

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

包絡、`max_offer_amount` 等其他欄位只能用 script 改（`--enabled false` 仍可用，作為 CLI 不可用時
停用的備用；`--enabled true` 被拒，啟用只走 `sudo bfx-op enable`，ADR 2026-10-15 D4）：dry run → 看報告 → 用 digest apply。在 VM 上以已部署的 backend image 跑一次性
container，用 `/opt/bfx/runtime/migrate.env`（owner role）。在正式環境，script 是 capital policy 的第二個寫入者，和 bot 用同一把 scope lock 序列化（ADR 2026-09-26 D1、ADR 2026-10-19 D4）：

```bash
python -m scripts.amend_capital_policy --exchange-account-id UUID --environment prod \
  --symbol fUST --max-offer-amount 200 --min-period-days 2 --max-period-days 2 \
  --max-open-offers 6 --rate-floor-ratio 0.5 --min-rate-apr 0.01
# 看完整報告後，同一組參數加 --apply-digest DIGEST --reason "為什麼改" 再跑一次
```

- apply 必須帶非空白的 `--reason`（dry run 不用）：owner 的 revision 沒有對應請求，原因只能記在 revision 的 `source`。

- 第一次設定包絡要五個欄位齊全（`envelope_incomplete` 會列出缺哪個）；之後可以只改其中一個。
- 利率下限 ＝ max(`min_rate_apr`/365, 即時 bid 中位數 × `rate_floor_ratio`)。低於下限時該幣別閒置，
  不是錯誤。
- 暫停／恢復一個幣別平常用上面的 CLI；script 的 `--enabled false` 是停用的備用（啟用沒有備用）。
  這是日常的停止方式，不需要 HALTED。
- 沒有包絡的幣別（舊 schema 的 policy）一律不送單（`envelope_unset`）。

## 3. 帳戶停止與恢復

### 主要：CLI（`sudo bfx-op`）；UI 只能 kill

```bash
sudo bfx-op resume --reason "為什麼恢復"   # 只走 CLI（ADR 2026-10-15 D4）
sudo bfx-op kill --reason "為什麼停"       # CLI 或 UI
sudo bfx-op status                         # 交易狀態、本次停機的 cancel-all、最近的請求
```

CLI 的入口、身分與 exit code 見 §10。請求排入 `trading_control_requests`，由 bot 的
`TradingControlWorker` 在 account lock 下重新驗證 operator 後套用；CLI 預設等到結果才結束。
webapi 停著時 CLI 照樣能用（它是 one-off container，直接連 Postgres）。

| 命令 | 效果 |
|---|---|
| `resume` | `HALTED` → `ACTIVE`（不論是誰停的）。不帶任何限額期：每筆單本來就在包絡內。已經 `ACTIVE` 時被拒（`already_active`） |
| `kill`（UI：Kill switch…，輸入 `KILL`） | `HALTED/operator` ＋ 每個幣別的 venue cancel-all，**連你在 Bitfinex 網頁手動掛的單也會撤**；已成交借款不受影響。等待中的其他請求標成 `superseded_by_kill`。kill 有自己的佇列、優先處理。已經 HALTED 時再送一次 ＝ 重試 cancel-all |

`status` 與 UI 面板的「本次停機的 venue 撤單」顯示 `funding_cancel_all_audit` 的結果（kill 請求觸發的列帶該請求的 `operator_request_id`，`/admin/halt` 與自動保護為 NULL）；不完整時重試 kill 或到
Bitfinex 手動撤單。**恢復只有 CLI 這一條路**（ADR 2026-10-15 D4）：UI 沒有恢復按鈕，HALTED 時只顯示 CLI 指令；
webapi 沒有 resume 端點，資料庫的 trigger 也拒絕 table owner 與 `bfx_operator` 以外的 role 排入 `resume`
（migration `36e59526f8cf`）；static token 不能恢復。

### 緊急備用：`sudo bfx-op halt`（只能降低曝險）

CLI 與 UI 都不可用時使用（例如只能從 Console 登入，§12）：

```bash
sudo bfx-op halt --reason "為什麼停"
sudo bfx-op --dry-run halt --reason "為什麼停"   # 只印出要跑的命令
```

`halt` 是唯一不要求 tailnet 身分的子命令：Tailscale SSH session 裡以你的 login 為 actor；Console
或 `tailscale whois` 失敗時照樣執行，actor 為 `host-root`，journal 標 `identity=none`。它不經 operator
container、不讀 ledger、`operator.env` 或資料庫，而是 `docker exec bfx-bot` 呼叫 bot healthz server
（container 內 port 8080，未對 host 公開）的 `POST /admin/halt`，與 kill 同一條路徑（HALTED ＋ 每個
幣別的 cancel-all）。token 在 container 內讀，不印出：9h 之後讀 bot 的秘密檔 `/run/secrets/BFX_ADMIN_TOKEN`，
9h 之前的 bot 讀 env。沒有 token 時 exit 1、什麼都不送。reason 由 bot 記進 `trading_state`，
不進 host journal。

| exit | 意義 |
|---|---|
| 0 | HALTED 且每個幣別 cancel-all 都 acknowledged（回應 body 印在 stdout） |
| 3 | HALTED 已生效，但 venue 的 cancel-all 沒有全部完成：同一個命令再跑一次即重試 |
| 1 | 其他回應或沒有回應（bot 沒在跑、token 不對等）：停機沒有確認 |
| 2 | 沒有 `--reason`、空白或超過 500 字；沒有用 sudo |

bot 自己停著時不可用（exit 1）：到 Bitfinex 網頁撤單並記錄。不依賴資料庫的 break-glass 是停掉 bot
container（見 §7）：停下的 bot 不會掛新單，但也不會撤單。

## 4. 異常的四個等級

| 等級 | 情況 | 反應 | 需要你做什麼 |
|---|---|---|---|
| 1 | 違反包絡、book 過期、auth DOWN、heartbeat 過期 | 擋那一筆 | 無 |
| 2 | 送單結果不明（UNKNOWN）、讀取失敗、未被接受的 snapshot | 只隔離該幣別；settle 窗口（120s）後以金額指紋＋完整 history 自動結案 | 30 分鐘仍未結案時收到 `unknown_quarantine_aged`（之後每 6 小時），見 §5 |
| 3 | `unclassifiable_commitment`、`offer_amount_mismatch`、`identity_conflict`、`venue_lent_above_ledger`、`command_rate_exceeded` | `HALTED/auto`，之後 planner 每一輪按 id 撤**受管** offer 直到沒有（外來 offer 不碰）；停滿 15 分鐘且連續 3 個乾淨 snapshot 後自動恢復（`ACTIVE/auto`），24 小時內最多 2 次 | 收到 `auto_resume_limit_reached` 或停機一直沒解除時：查清原因後以 CLI 恢復（§3） |
| 4 | 外來 offer（`foreign_exposure`）、外來 offer 成交造成的借出（`foreign_lending`）、NAV 下降（`nav_drop`） | 只告警 | 確認是不是你自己的操作 |

- **不會**觸發任何反應：借款到期造成 lent 減少、reconcile 補回 WS 漏掉的成交或撤單。
- writer lock 遺失不是交易決策：bot 直接退出、container 重啟（Telegram 收到 `daemon_fatal`）。重啟後若鎖仍被
  另一個 writer 持有，開機以 exit 75 退出，由 restart policy 反覆重啟再試，直到鎖釋放；開機不會在 process
  內等鎖。
- 開機後在收到 Bitfinex 第一個 frame 之前不送單；它與資料庫第一次 ping 成功之前，`/readyz` 為 not ready（`dependency_stale`）。
- Bitfinex 連不上不會重啟 bot：由 heartbeat guard 與 book 過期擋單，`/readyz` 轉成 not ready（reason
  `dependency_stale`），連線回來就恢復。資料庫斷線時 audit 寫不進去而擋單；斷線若撐過一次 writer lock
  refresh（30 秒），仍會因 writer lock 遺失（見上）而退出。開機時 Bitfinex 連不上，或資料庫在開機第一次
  連線時就連不上，則在 process 內重試等待（Telegram 收到一次 `boot_waiting`），不會反覆重啟。資料庫若在
  開機觀測進行中才斷，會因 writer lock 遺失退出一次（`daemon_fatal`、`boot_refused`），重啟後再於第一次連線處等待。
  Bitfinex 拒絕 API key（HTTP 500 帶 `["error", 10100, ...]`）不等待也不結束，改進憑證恢復模式（見 `credential_recovery_mode`）；其他明確拒絕（例如 nonce 10114）不會等待，直接拒絕開機。
- event loop 卡住超過 `BFX_LOOP_WATCHDOG_S`（預設 120 秒）：bot 把所有 thread 的 traceback 寫到 container
  log 後直接退出、container 重啟。退出當下來不及發 Telegram；重啟後的開機會發現上一個 run 沒有結束紀錄，
  發一次 `previous_run_unclean`（附 run id、開始時間、發現時間），在 container log 的這段時間找 `Timeout`
  段落（watchdog）；沒有 `Timeout` 則多半是 OOM kill 或 SIGKILL（`docker inspect` 的 `OOMKilled`）。
- 拒絕開機（schema 與 build 不符、policy 讀不到）：只發 `venue_offers_may_remain` 然後退出，
  不寫交易狀態、不碰 venue；修好後的下一版會照常開機。venue 上已有的受管 offer 仍在包絡內；要撤就用 kill。
- 第 3 級自動恢復：衝突類（`offer_amount_mismatch`、`identity_conflict`、`unclassifiable_commitment`）條件還在就一直
  停著；`venue_lent_above_ledger` 與 `command_rate_exceeded` 停機後偵測不到，15 分鐘內沒再發生就恢復，
  一天第 3 次停機才要人。恢復時 Telegram 會收到 `trading state HALTED -> ACTIVE`（cause `auto`，actor `auto-resume`，
  reason 列出停機原因、停了幾分鐘與 3 個乾淨 snapshot 的 event_seq）。在 HALTED/auto 期間按 kill 會改寫成
  `HALTED/operator`，之後不會自動恢復。log：`auto_resumed`、`auto_resume_refused`。
- 第 3 級處理步驟（自動恢復沒發生、或收到 `auto_resume_limit_reached`）：
  1. 看 Telegram 與 `sudo bfx-op status`（或 UI）的 cause/reason；container log 用 `docker logs bfx-bot --since 1h`。
  2. 看 log 的 `managed_offer_sweep`（requested／failed）；撤不掉的受管 offer 每一輪會重試，急的話用 kill。
  3. 查清 ledger 與 venue 的差異（`/admin/trading-status`、`/admin/dry-evaluate`）。
  4. 原因消除後 `sudo bfx-op resume --reason …`（§3）。

## 5. UNKNOWN 的人工處理

絕大多數 UNKNOWN 在 settle 窗口後自動結案：找到帶同一指紋、同條件的 offer 就認領；history 完整
涵蓋送單時間卻沒有，就判定未送出。以下情況會維持隔離、等你處理：history 不完整、兩個以上候選、
候選已被其他紀錄認領、或指紋相同但其他條件不符。

裁決只走 CLI（ADR 2026-10-15 D4）：UI 只顯示證據、候選與請求結果，下一步顯示 CLI 指令；webapi 沒有裁決端點，
對 `uncertainty_resolution_requests` 也沒有 INSERT 權限（migration `36e59526f8cf`）。請求排入 `uncertainty_resolution_requests`，
由 bot 套用；需要新鮮的 reconcile fence，過期會被拒 `stale_reconcile_fence`：

```bash
sudo bfx-op uncertainties --state open        # 未結案的 uncertainty
sudo bfx-op uncertainty <uncertainty_id>      # 單筆：候選、引用的 evidence ref、最近的請求
sudo bfx-op bind-to-venue <uncertainty_id> --evidence-ref <ref> --venue-offer-id <id> [--reason …]
sudo bfx-op mark-not-accepted <uncertainty_id> --evidence-ref <ref> [--reason …]
sudo bfx-op manual-resolution <uncertainty_id> --evidence-ref <ref> \
  --decision closed_at_venue|accepted_external_exposure --reason …
```

`--evidence-ref` 照抄 `uncertainty` 輸出裡的值；CLI 不接受自己組的 evidence。

| uncertainty kind | 可用動作 |
|---|---|
| `submit_outcome_unknown`（UNKNOWN 送單） | `bind-to-venue`：綁到 venue 上唯一且完全符合的 offer；`mark-not-accepted`：有完整 venue history 證明 venue 沒收下 |
| `unattributed_venue_offer`、`unsupported_venue_exposure`（2026-09-25 前留下的舊紀錄） | `manual-resolution`：`closed_at_venue` 或 `accepted_external_exposure`。新的外來 offer 不再產生這種紀錄；ledger 只從 attempt 與 quarantine 產生 uncertainty，舊表 `execution_uncertainties` 已不讀，所以 API 與 UI 不再列出這兩種 |

`uncertainties` 回 `uncertainties_unavailable` 時看輸出的 `cause` 與 `sqlstate`：`42501` 是 `bfx_operator` 少了讀取權限（migration 的 grant 沒補上），不是一般的拒絕。

規則：

- 零個或多個候選 ＝ 維持 UNKNOWN，不要猜。不要刪資料列、不要自己合成 venue reference。
- 需要 DB 還原或 venue 寫入之後的回滾判斷時，改走
  [rollback after a venue write](rollback-after-venue-write.md)。

## 6. 告警

兩條 Telegram 路徑，**用同一組 bot token 與 chat id**：

| 來源 | 設定檔 | 內容 |
|---|---|---|
| bot 內的 sink（`observability/alerts.py`，非阻塞） | 秘密檔 `/opt/bfx/runtime/secrets/bot/TELEGRAM_BOT_TOKEN` 與 `bot.env` 的 `TELEGRAM_CHAT_ID` | 交易狀態轉換、自動保護、kill 結果、恢復結果、幣別啟停結果（`capital_policy_request_*`）、憑證輪替結果（`credential_rotation_*`）、key 重查持續失敗（`credential_recheck_failing`）、`foreign_exposure`、`foreign_lending`、`unknown_quarantine_aged`、`nav_drop`、`venue_offers_may_remain` |
| host 工具 `bfx-notify` | `/opt/bfx/runtime/notify.env`（同樣兩個 key） | bfx-deploy 結果、bfx-backup-check（RPO>300s、evidence 超過 15 分鐘、restore heartbeat 超過 35 天、磁碟 ≥95% 為 critical；restore timer 停用、weekly report 超過 8 天沒成功、磁碟 ≥85% 為 warning）、`bfx-alert@` 的 unit 失敗（pgBackRest 備份、restore test） |

- 兩個都空 → bot 只記 log（開機時會說一次）；`bfx-notify` 永遠 exit 0，送不出去只記 journal。
- **換 token 或 chat id 兩條路徑都要改**：改 `notify.env` 立即生效；bot 這一側改完後要
  `sudo bfx-deploy --recreate`（§7）。換 token 的步驟見 [secret-rotation](secret-rotation.md#telegram-bot-token)。
- **為什麼是兩條、兩份檔**（design review #14，刻意保留）：兩條路徑在容器／主機的邊界兩側，
  各自要在對方掛掉時還能發出告警——bot 死掉或起不來時由主機的 bfx-deploy／`bfx-alert@` 通報，
  主機 timer 停擺時交易事件仍由 bot 自己通報；合成一條就會讓其中一邊失去告警。token 分兩份檔是
  最小權限：bot 的秘密檔只掛給 bot container，`notify.env` 只給 root 的主機工具，DR unit（`ubuntu`）
  兩份都讀不到。
- 測試 host 路徑：`sudo bfx-notify --level info "test"`。
- <a id="alert-alert_test"></a>測試 bot 路徑（`alert_test`，info）：在執行中的 bot container 裡跑
  `sudo docker exec bfx-bot /app/.venv/bin/python -m scripts.send_test_alert`。它用 bot 自己的設定
  （秘密檔與 `bot.env`）建 sink，經 `alerts.emit` 送出（dedup、速率限制、secret 遮罩都照常），不碰資料庫與 venue；
  Telegram 收下時 exit 0，只記 log（key 缺或格式錯）或送不出去時 exit 1。
- Grafana 仍是第二條觀測路徑。
- **停機後仍送單**（Grafana 規則 `bfx-trading-halted-submits`，critical）：bot 的 `/metrics` 匯出
  `bfx_trading_halted`（與 `/admin/trading-status` 的 `halt.halted` 同一個 guard 判定），規則在它連續
  6 分鐘為 1、且 `bfx_executor_submits_total{status!="not_sent"}` 最近 5 分鐘有增加、且這個狀況持續 2 分鐘時觸發（gauge 只在 scrape 時更新，2 分鐘避開恢復交易後的第一次送單）。`not_sent`
  是 guard 擋下、沒送出的；其餘狀態代表送單已離開 bot。halt 要先持續過整個視窗，才不會把 halt 前
  幾分鐘的正常送單當成違規；代價是 halt 後約一分鐘內的單筆送單不在偵測範圍（halt 若真的失效，之後
  每一輪都會再送單而被抓到）。
  - 系統自己會做的：command gate 在 halt 時拒絕新送單，這條告警只做事後偵測，不改交易狀態。
  - operator 要判斷的：用 `/admin/trading-status` 看 `halt` 與 `last_submit_attempt`，確認送出的是哪一筆、
    為什麼通過 gate；在找到原因前保持 halt。若 venue 上有不該存在的 offer，走 kill（§3）。

### Container log 的格式與安全事件

`bfx-bot` 與 `bfx-webapi` 的 log 每行是一個 JSON 物件（寫 stderr，`docker logs` 與 homelab alloy 照常讀得到）：
`ts`（UTC，結尾 `Z`）、`level`、`service`（`bot`／`webapi`）、`logger`、`msg`（訊息文字）、有 traceback 時的 `exc`，
以及事件自己的欄位（`event`、`reason` 等）。值裡的換行與控制字元都被跳脫，一筆 log 只佔一行；例外是 bot 未攔下的例外在程序結束時由直譯器印出的多行純文字 traceback。

- 照舊用事件名 grep：事件名在 `msg` 裡，例如 `docker logs bfx-bot --since 1h 2>&1 | grep deployment_skip`。
  Grafana 的 bfx 規則比對整行原文，不受格式影響。
- 取欄位用 `jq`（要先裝）：`docker logs bfx-webapi --since 1h 2>&1 | jq -R -c 'fromjson? | select(.logger == "bfx.security")'`（`fromjson?` 跳過非 JSON 行）。
  operational telemetry（logger `bfx_funding_bot.events`）的事件 JSON 是 `msg` 字串，要再解一次：
  `jq -R -c 'fromjson? | select(.logger == "bfx_funding_bot.events") | .msg | fromjson'`。
- web API 的 access log（logger `uvicorn.access`）的 client 是 Serve 代理轉來的位址（`127.0.0.1`，或 Serve 帶 `X-Forwarded-For` 時的 tailnet IP），不代表身分；看身分要看安全事件。

安全事件（logger `bfx.security`；查法 `docker logs bfx-webapi --since 24h 2>&1 | grep bfx.security`，bot 同理）：

| `event` | 什麼時候 | 主要欄位 |
|---|---|---|
| `access_denied` | web API 401（`operator_identity_missing`、`operator_required`）、帳戶不是成員（`account_not_found_or_not_member`，回應是 404）、viewer 寫入（`account_write_required`）；bot `/admin/*` 401／403（`missing_auth`、`unauthorized`） | `reason`、`method`、`path`、`login`、`tailnet_user_ids`、`user_id`、`exchange_account_id`；bot 另有 `client` |
| `request_rejected` | ingress 拒絕：`host_not_allowed`（400）、`cross_site_refused`（403）、`json_required`（415） | `reason`、`status`、`header`（失敗的 header）、`value`、`login` |
| `rate_limited` | 429 | `user_id`、`login`、`reason`（`read_bucket_empty`／`write_bucket_empty`）、`retry_after_s` |
| `boot_refused` | 拒絕開機（CRITICAL）：web API 的 `admin_token_in_webapi_env`、`ingress_settings_invalid`、`schema_head_mismatch`、`capital_authority_unreadable`；bot 的 `boot_failed`（只有 `error_types`，訊息在 Telegram 的 `boot_refused` 告警） | `reason`、`detail`、`error_types` |
| `admin_halt` | bot `/admin/halt` 被呼叫 | `actor`、`halt_reason`、`cancel_all_complete` |

log 裡沒有 token、帶 token 的 header 或 request body；request 帶來的字串截到 200 字。

### Error tracking（Sentry）

bot、webapi 與營運 UI（瀏覽器）的未處理例外與 ERROR log 送到 Sentry SaaS（org `will-me`；project `bfx-backend` 收 bot 與
webapi，以 `service` tag 區分；`bfx-frontend` 收瀏覽器）。Sentry 依 fingerprint 分組，
只在**新 issue、回歸（resolved 後再出現）、或同一 issue 一小時超過 100 次**時寄 email；它不是 page 類告警，
交易與營運異常仍由上面的 Telegram 與 Grafana 通報。

| 服務 | 設定 | 關閉方式 |
|---|---|---|
| bot、webapi | `/opt/bfx/runtime/bot.env`、`webapi.env` 的 `SENTRY_DSN`（同一個 `bfx-backend` DSN） | 清空 `SENTRY_DSN` 後 `sudo bfx-deploy --recreate`（§7） |
| 營運 UI（瀏覽器） | build 時的 repository variable `NEXT_PUBLIC_SENTRY_DSN`（`release.yml` 缺值就失敗，build 進 backend image）；webapi 的 `BFX_FE_SENTRY_DSN`（`/fe-events` 唯一允許的轉送目標，必須是同一個 DSN） | 改 variable 後重發 release |

- 送出的內容（`modules/observability/error_tracking.py`、`frontend/src/lib/sentry.ts`）：
  - 每個字串都會去掉 `alerts.secret_values` 列出的值（含 DB 密碼與 DSN key），以及 email 與 IP。
  - `user`、cookie、`Authorization` 等 header 整個丟掉。
  - 不送 local variables，也不送 request body。
  - 遮罩失敗時整筆不送。
- 送出由 SDK 的背景 thread 處理，queue 有上限，不在交易路徑上等待網路；結束時最多 flush 2 秒。
  outgoing request 不加 trace header。
- 瀏覽器的事件先送到本站的 `/fe-events`（`frontend/src/lib/sentry-tunnel.ts`），再由 webapi 轉給 `BFX_FE_SENTRY_DSN`
  的 project（`backend/.../modules/api/ingress.py`；只接受同源、DSN 相符的 envelope）；
  廣告與隱私擴充套件會擋直接送往 `*.sentry.io` 的請求（console 顯示 `ERR_BLOCKED_BY_CONTENT_BLOCKER`）。
- <a id="error-tracking-test"></a>測試：在執行中的 container 裡各送一筆測試例外：
  `sudo docker exec bfx-bot /app/.venv/bin/python -m scripts.send_test_error --service bot`
  （webapi 用 `bfx-webapi`、`--service webapi`）。exit 0 並印出 event id 後，到 Sentry 確認 issue
  `ErrorTrackingTestError` 出現，而且訊息裡的 email 與 IP 顯示為 `[redacted]`；確認後把 issue resolve。
  `SENTRY_DSN` 未設時 exit 1。
  營運 UI：從 tailnet 打開 UI 後在瀏覽器 devtools console 執行
  `setTimeout(() => { throw new Error("frontend error test error-test@example.com") })`，
  在 `bfx-frontend` 確認 issue 出現且 email 為 `[redacted]`。
- operator 要判斷的：新 issue 是程式錯誤還是外部服務（venue、DB）的暫時失敗。前者開 issue 修；後者若
  已有對應的 Telegram 告警或自動恢復，就把它在 Sentry 設為 ignore（直到再次回歸）。

### bot 告警事件

bot 發出的每個事件都登錄在 `observability/alerts.py` 的 `ALERT_EVENTS`，並在下面有一段（測試
`tests/architecture/test_alert_runbook_coverage.py` 強制兩者一致）。同一事件同一組識別欄位 10 分鐘內只送
一則；dedup 狀態只存在記憶體，bot 重啟後重新計算。

<a id="alert-trading_state_changed"></a>
#### `trading_state_changed`（HALTED 時 critical，ACTIVE 時 info）

- 代表：`trading_state` 寫入一筆新的轉換（自動保護停機、自動恢復、CLI 恢復、CLI／UI kill、`/admin/halt`）；標題 `trading state X -> Y`，附 cause、actor、reason、state_id。重述同一狀態（例如已 HALTED 時再 kill）不寫新列，也不發。
- 系統會做：HALTED 後所有幣別不掛新單，planner 每一輪撤受管 offer；`HALTED/auto` 依 §4 第 3 級條件自動恢復，`HALTED/operator` 不會。
- operator 判斷：看 cause／actor。`auto` 停機走 §4 第 3 級；`operator` 停機確認是不是你（同時應有 `trading_control_applied` 或 `kill_switch_engaged`）；`ACTIVE/auto`（actor `auto-resume`）是預期的恢復，不必處理。

<a id="alert-protection_tripped"></a>
#### `protection_tripped`（critical）

- 代表：第 3 級自動保護觸發（`unclassifiable_commitment`、`offer_amount_mismatch`、`identity_conflict`、`venue_lent_above_ledger`、`command_rate_exceeded`），標題 `automatic HALT: …`，`detail` 是觸發細節。同一 trigger 10 分鐘內只發一次；已經 HALTED 時條件持續也會發（受同一 dedup 限制）。
- 系統會做：guard 立刻擋新單，背景 task 寫 `HALTED/auto`（寫入失敗每 5 秒重試，期間仍擋單），接著收到 `trading_state_changed`；planner 按 id 撤受管 offer，外來 offer 不碰；停滿 15 分鐘且連續 3 個乾淨 snapshot 後自動恢復，24 小時內最多 2 次。
- operator 判斷：沒有跟著收到 `trading_state_changed` 時，查 log `automatic_protection_engage_failed`（HALTED 寫不進 DB）。其餘照 §4 第 3 級處理步驟。

<a id="alert-kill_switch_engaged"></a>
#### `kill_switch_engaged`（cancel-all 完整時 warning，不完整時 critical）

- 代表：operator 的 kill（CLI／UI kill、`/admin/halt`，或 `kill_cancel_all_caught_up` 補跑）寫完 `HALTED/operator` 並對每個幣別跑完 venue cancel-all。`complete=false` 時 `not_acknowledged` 列出沒被確認的幣別與 phase，`scope_error` 表示幣別清單讀不完整。自動保護不會走到這裡。
- 系統會做：HALTED 維持；結果寫入 `funding_cancel_all_audit`。不完整的 cancel-all 不會自己重試（只有 process 中斷造成的未完成會由 `kill_cancel_all_caught_up` 補跑）。
- operator 判斷：不完整時看 `sudo bfx-op status`（或 UI）的「本次停機的 venue 撤單」，照 §3 重試 kill；同一 state_id 同樣結果 10 分鐘內不會再告警，以 `status` 為準。phase 為 `skipped`（`no_live_venue`、`writer_lock_not_held`）時先處理 bot 本身。不是你發的 kill：確認 `actor`。

<a id="alert-boot_refused"></a>
#### `boot_refused`（critical）

- 代表：bot 在開機完成前失敗（exit 1）：watchdog 設定無效、`build_daemon` 拋錯（schema／realm／ledger epoch、policy 讀不到、venue wiring、vault 沒有可用的 active 憑證或 KEK 不符、資料庫非暫時性錯誤），或開機觀測遇到非暫時性錯誤（例如 nonce 10114、invariant）。venue 拒絕 API key 不在此列：bot 不結束，改發 [`credential_recovery_mode`](#alert-credential_recovery_mode)。`error` 是例外摘要。
- 系統會做：process 退出，不寫交易狀態、不碰 venue；`restart: unless-stopped` 重啟 container。組裝 daemon 時的拒絕（schema／realm／ledger epoch、policy、venue wiring）會先發 `venue_offers_may_remain`；帳戶設定、watchdog 設定與開機觀測只有這則。dedup 只在同一 process 內，crash loop 時每次重啟都會再收到一則。
- operator 判斷：看 `error` 與 `docker logs bfx-bot`。schema／build 不符屬部署問題（[deploy runbook](deploy.md)）；nonce 10114 見 §9。修好後下一版照常開機，不需要 resume。writer lock 被另一個 writer 持有是 exit 75，不發這則（§4）。

<a id="alert-boot_waiting"></a>
#### `boot_waiting`（warning）

- 代表：開機時第一次連資料庫（`step=database`）或開機觀測（`step=venue observation`）遇到暫時性連線錯誤；每次等待只在第一次失敗時發一則。
- 系統會做：在 process 內以 1、2、4…最多 60 秒的 backoff 重試，不重啟；等待期間沒有任何交易 task，`/readyz` not ready；開機觀測每次重試前會重新確認 writer lock。連線回來後照常開機，不另發告警（log `boot_dependency_reachable`）。
- operator 判斷：確認是 Bitfinex 端還是 `bfx-postgres`（`docker ps`、`docker logs bfx-postgres`）。很快恢復就不用處理；一直等待時處理依賴本身，不要重啟 bot（重啟只會再等一次）。

<a id="alert-previous_run_unclean"></a>
#### `previous_run_unclean`（critical）

- 代表：開機時發現同一 scope 有沒有結束紀錄的 run（`bot_runs.end_reason` 為空），也就是上一個 process 沒走到正常收尾：loop watchdog（`BFX_LOOP_WATCHDOG_S`，預設 120 秒）、OOM kill、SIGKILL 或 crash。每個未結束的 run 發一則，附 run_id、started_at、noticed_at、host。
- 系統會做：把那些 run 標成 unclean 後照常開機；當時送出但沒記下結果的單，開機 recovery 會轉成 UNKNOWN 並隔離該幣別（§4 第 2 級）。
- operator 判斷：照 §4 的 watchdog 條目，在 container log 的 started_at～noticed_at 之間找 `Timeout` 段落；沒有的話看 `docker inspect` 的 `OOMKilled`。反覆發生就查記憶體或卡住 event loop 的原因。

<a id="alert-daemon_fatal"></a>
#### `daemon_fatal`（critical）

- 代表：已完成開機的 bot 因致命錯誤停下：writer lock refresh 後仍未持有、送單結果無法寫入 DB（`submit_outcome_lost`）、periodic reconcile 的讀取或送單、或每 6 小時的 key 重查遇到 venue 拒絕 API key（`VenueCredentialRejectedError`，exit 78）、key 重查發現 key 不合規（`CredentialNoncompliantError`，exit 78），或任一 sub-task 拋出例外（exit 1）。`error` 說明是哪一種。
- 系統會做：process 退出、container 重啟；不寫交易狀態。結果沒寫下的送單在重啟後變成 UNKNOWN 並隔離該幣別（§4 第 2 級）；lock 仍被其他 writer 持有時開機以 exit 75 反覆重試。
- operator 判斷：看 `error` 與 `docker logs bfx-bot`。資料庫斷線造成的 lock 遺失在 DB 恢復後自己好；`VenueCredentialRejectedError` 或 `CredentialNoncompliantError` 時重啟後的開機會進憑證恢復模式，接著收到 [`credential_recovery_mode`](#alert-credential_recovery_mode)，照那則處理；同一錯誤反覆出現就是 bug，要查清楚。venue 上的受管 offer 仍在包絡內，要撤用 kill（§3）。

<a id="alert-foreign_exposure"></a>
#### `foreign_exposure`（warning）

- 代表：venue 上有一筆 active offer，沒有任何 durable intent 對得上（例如在 Bitfinex 網頁掛的單、自動續借、同一把 key 的其他程式，§9）；runtime 在 offer 建立超過 120 秒後才判定，開機時立即判定。每個 venue offer id 在同一 process 只發一次。
- 系統會做：只告警（§4 第 4 級）：不撤、不算進受管 offer、不停機。只有 kill 的 cancel-all 會撤它。
- operator 判斷：確認是不是你自己的操作或你啟用的自動續借。都不是的話視為 API key 外洩：走 kill（§3）並更換 key。

<a id="alert-unknown_quarantine_aged"></a>
#### `unknown_quarantine_aged`（warning）

- 代表：一筆 UNKNOWN 送單讓該幣別隔離已超過 30 分鐘；仍未結案時每 6 小時再提醒一次。附 symbol、attempt_id、minutes、amount。
- 系統會做：該幣別維持不掛新單，其他幣別照常；每個被接受的 observation 都會再試一次自動結案。不會升級成停機。
- operator 判斷：照 §5 處理（Overview 的 uncertainty 明細）。

<a id="alert-auto_resume_limit_reached"></a>
#### `auto_resume_limit_reached`（critical）

- 代表：一次 `HALTED/auto` 的條件已消失（停滿 15 分鐘、3 個乾淨 snapshot），但 24 小時內已自動恢復 2 次，DB trigger 拒絕第 3 次。每個停機 id 在同一 process 只發一次，附原停機的 reason。
- 系統會做：維持 HALTED，planner 繼續撤受管 offer，不再嘗試自動恢復。
- operator 判斷：一天內第 3 次停機代表原因會反覆出現。照 §4 第 3 級處理步驟查清原因，再以 CLI 恢復（§3）。

<a id="alert-reconcile_not_accepted"></a>
#### `reconcile_not_accepted`（warning）

- 代表：週期 reconcile（`BFX_RECONCILE_INTERVAL_S`，預設 90 秒）連續 3 個 cycle 沒有被 ledger 接受；`decision` 為 `incomplete_or_unequal`（history 不完整或兩次觀測不同）、`fenced`（觀測期間 command clock 前進）或 `query_admission_refused`（scope 內有還沒有結果的 attempt）。streak 剛到 3 時發一次，被接受一次後才會重新計數。
- 系統會做：RECONCILE 健康狀態轉為 DEGRADED；沒被接受的 cycle 跳過 deployment，所以不掛新單、不重訂價，也不跑停機或停用後的受管 offer 撤單。下一個被接受的 cycle 就恢復。
- operator 判斷：依 `decision` 找原因：`incomplete_or_unequal` 看同時段的 `venue_observation_history_incomplete`；`query_admission_refused` 看是否有一直沒結果的送單。持續不恢復且需要撤 venue 上的 offer 時走 kill（§3）。

<a id="alert-offer_end_unconfirmed"></a>
#### `offer_end_unconfirmed`（warning）

- 代表：一筆 offer 從 active 消失，依 id 向 venue 點查超過 120 秒 grace 仍沒有 terminal 答案；`cause` 為 `not_returned`、`request_failed`、`cap_exhausted` 或 `unparseable`。grace 從本 process 第一次看到該 id 沒有答案時起算。
- 系統會做：observation 把它記為 unconfirmed end（不寫 terminal 列），讓 observation 可以被接受；conservation 只用這筆 offer 自己的 funding trades 判斷成交量，判完發 `offer_end_judged`。
- operator 判斷：通常不用處理，等 `offer_end_judged`。`cause` 一直是 `request_failed` 或 `cap_exhausted` 時，表示 venue API 或 request budget 有問題，要查 log。

<a id="alert-offer_end_judged"></a>
#### `offer_end_judged`（explained 時 info，undeterminable 時 warning）

- 代表：conservation 對 unconfirmed end 的判決已寫入：`outcome=explained`（trades 定出成交量 `filled`）或 `undeterminable`（trades 定不出來，衝突仍在）；`verdict` 是該幣別這次的 conservation 判決。
- 系統會做：explained 照常對帳。undeterminable 的幣別判為 `unexplained_lending`，capital read 回 `venue_lent_above_ledger`，接著 `protection_tripped` 與 `HALTED/auto`，之後依 §4 第 3 級自動恢復。
- operator 判斷：explained 不用處理。undeterminable 照 `protection_tripped` 處理（§4）；同一情況反覆出現，表示只靠 trades 判斷的規則需要重新評估，要記錄下來交給開發處理。

<a id="alert-provenance_conflict"></a>
#### `provenance_conflict`（warning）

- 代表：要撤某幣別的受管 offer（帳戶 HALTED 或該幣別停用）時，有一筆 live offer 的 provenance 自相矛盾，sweep 分不出哪些 offer 是 bot 的。同一（symbol、offer id）只發一次，直到該幣別撤單成功一次才重新計算。
- 系統會做：該幣別這一輪什麼都不做（不撤也不掛），下一輪再讀一次；其他幣別照常。
- operator 判斷：持續出現代表該幣別的受管 offer 撤不掉；需要撤時走 kill（§3，cancel-all 不看 provenance）。這是 ledger 與 venue 的紀錄矛盾，要附 offer id 回報成 bug。

<a id="alert-nav_drop"></a>
#### `nav_drop`（warning）

- 代表：某幣別的 NAV（available＋offered＋lent，原生單位）24 小時內從高點下降超過 `realized_loss_24h_pct`（5%），或從歷史高點下降超過 `drawdown_pct`（10%）（`backend/configs/safety.live.yaml` 的 `nav_alerts`）。每個（幣別、指標）每次超標只發一次，回到門檻內才重新計算。
- 系統會做：只告警（§4 第 4 級），放貸照常；利率不好只會少賺，不會讓 NAV 下降。
- operator 判斷：確認是不是你自己的提領或轉帳。都不是的話，可能是平台損失或帳戶被動用：後者走 kill（§3）並更換 key。

<a id="alert-foreign_lending"></a>
#### `foreign_lending`（warning）

- 代表：被接受的 basis 判定某幣別的新增借出無法用 bot 的成交解釋，但可以用該期間成交的外來 offer 涵蓋（超過 0.01）；`detail` 附 `lent_unexplained` 與 `foreign_executed`。每個 basis 只判一次。
- 系統會做：只告警（§4 第 4 級），不停機。外來 offer 涵蓋不了的部分會變成 `venue_lent_above_ledger` 而停機，不是這則。
- operator 判斷：同 `foreign_exposure`：確認是不是你自己的操作或自動續借；都不是的話走 kill（§3）並更換 key。

<a id="alert-venue_offers_may_remain"></a>
#### `venue_offers_may_remain`（critical）

- 代表：組裝 daemon 時開機被拒（資料庫非暫時性錯誤、schema 與 build 不符、realm 或 ledger epoch 不對、applied policy 讀不到、venue wiring 失敗；模擬環境同樣會發），bot 停機前 venue 上的受管 offer 可能還在。`reason` 是拒絕原因。
- 系統會做：不寫交易狀態、不碰 venue，接著發 `boot_refused` 並退出，container 重啟。留在 venue 上的 offer 仍在當初的包絡內。
- operator 判斷：先修好原因（多半是部署，見 [deploy runbook](deploy.md)），修好後的版本照常開機、不需要 resume。必須撤單時照 §3 處理；bot 沒在跑時 `bfx-op halt` 不可用。

<a id="alert-command_throttled"></a>
#### `command_throttled`（warning）

- 代表：command gate 的 token bucket（`pre_trade_limits.command_rate`：容量 12、每秒補 0.1，約每分鐘 6 筆）用完時又來一筆送單，送單被擋（`command_rate_limited`）。附 `blocked_in_window`（300 秒內被擋的送單數）。被擋的撤單只記 log，不發告警、也不計入。
- 系統會做：被擋的送單在寫入任何 durable 紀錄前就被擋下，之後再送；300 秒內被擋 6 筆就觸發 `protection_tripped`（`command_rate_exceeded`）並進入 `HALTED/auto`。
- operator 判斷：開機或重訂價時偶爾一筆不必處理。`blocked_in_window` 一直增加代表有失控的送單迴圈，用 `docker logs bfx-bot` 找是哪個幣別、哪個 cell。不要用調高 limit 繞過。

<a id="alert-trading_control_applied"></a>
#### `trading_control_applied`（warning）

- 代表：CLI 的恢復、CLI／UI 的 kill 請求已由 `TradingControlWorker` 套用；`outcome` 是 `resumed` 或 `HALTED; venue cancel-all follows`，`by` 是請求者。kill 時帳戶本來就 HALTED 的話，`state_id` 是既有那一列，不會接著收到 `trading_state_changed`。
- 系統會做：狀態有變時接著發 `trading_state_changed`；kill 接著跑 cancel-all，結果見 `kill_switch_engaged`。
- operator 判斷：確認 `by` 是你。不是的話代表有人用 operator 權限操作：先 kill（§3）再查帳號。

<a id="alert-trading_control_rejected"></a>
#### `trading_control_rejected`（warning）

- 代表：恢復或 kill 請求被拒，`reason` 為 `operator_not_authorized`（請求者已不是有效的 operator）、`already_active`（帳戶已是 ACTIVE 時要求恢復）或 `illegal_transition: …`（DB 規則拒絕）。被 kill 取代的請求（`superseded_by_kill`）只記在請求上，不發這則。
- 系統會做：狀態不變，請求就此結案，不會重試。
- operator 判斷：依 `reason` 處理；修正後以 CLI（§3）重新送出。

<a id="alert-trading_control_failed"></a>
#### `trading_control_failed`（critical）

- 代表：套用恢復或 kill 時發生錯誤（`reason` 為 root exception 類型，或 `outcome_write_failed:…`），或 kill 已寫入 HALTED 但 kill switch 沒接上（`kill switch not wired: venue offers not cancelled`）。
- 系統會做：一般失敗時整個套用 rollback，交易狀態不變（失敗的 kill **沒有**停機），請求標為 failed、不重試。kill switch 沒接上時 HALTED 已生效，但 venue 沒有撤單。
- operator 判斷：如果失敗的是 kill，馬上用 `/admin/trading-status` 看 `halt`，沒停就重送 kill 或用 `sudo bfx-op halt`（§3）；cancel-all 沒跑就重試 kill。原因看 `docker logs bfx-bot` 的 `trading_control_failed`。

<a id="alert-kill_cancel_all_caught_up"></a>
#### `kill_cancel_all_caught_up`（critical）

- 代表：閒置的 worker 發現最近一次已套用的 kill 仍在生效，但之後沒有完成的 cancel-all（process 在 commit 後死掉，或某幣別的 audit 停在 `requested`）。每個 kill 在同一 process 只補一次。
- 系統會做：立刻補跑 cancel-all，結果見接著發的 `kill_switch_engaged`。
- operator 判斷：看 `kill_switch_engaged` 是否完整，不完整照 §3 處理。同時段若有 `daemon_fatal` 或 `previous_run_unclean`，一起查原本的 cancel-all 為什麼中斷。

<a id="alert-capital_policy_request_applied"></a>
#### `capital_policy_request_applied`（warning）

- 代表：CLI（或 UI 停用）的幣別啟用或停用請求已套用；`outcome` 為 `enabled (revision N)`、`disabled (revision N)`、`enabled; envelope unset: …` 或 `unchanged: already …`（沒寫新 revision）。附 symbol 與 `by`。
- 系統會做：停用後下一個 reconcile tick 撤該幣別的受管 offer；啟用後 planner 開始考慮該幣別，沒有包絡時每一筆單都會被 envelope guard 拒絕（§2）。
- operator 判斷：確認 `by` 是你。看到 `envelope unset` 時，用 `amend_capital_policy` 設好包絡（§2）。

<a id="alert-capital_policy_request_rejected"></a>
#### `capital_policy_request_rejected`（warning）

- 代表：幣別啟停請求被拒，`reason` 為 `operator_not_authorized`、`policy_unavailable`（該幣別沒有 applied policy）、`unsupported_enabled_symbol`（例如 fUSD），或其他 policy 拒絕代碼。被 kill 取代的啟用請求（`superseded_by_kill`）不發這則。
- 系統會做：不寫 revision，請求就此結案。
- operator 判斷：依 `reason` 處理（§2）；修正後重新送出。

<a id="alert-capital_policy_request_failed"></a>
#### `capital_policy_request_failed`（critical）

- 代表：套用幣別啟停時發生錯誤（`reason` 為 root exception 類型或 `outcome_write_failed:…`）。
- 系統會做：rollback，不寫 revision，請求標為 failed、不重試。失敗的**停用**代表該幣別仍在放貸。
- operator 判斷：以 `sudo bfx-op status` 確認該幣別的實際狀態並重送（§2）；停用失敗而且必須馬上撤單時走 kill（§3）。原因看 `docker logs bfx-bot` 的 `capital_policy_request_failed`。

<a id="alert-credential_rotation_applied"></a>
#### `credential_rotation_applied`（warning）

- 代表：operator CLI 的 `rotate-credential` 已套用：新 key 的權限只有 funding 可寫、跟現行 key 屬於同一個 Bitfinex 帳號；舊憑證（`replaces_credential_id`）已 retired，新憑證（`credential_id`）是唯一的 active，`reason` 為 `rotated: credential <id>`。
- 系統會做：commit 後 bot 照 SIGTERM 的乾淨停機流程結束（log `credential_rotation_restart`，`bot_runs` 記 `clean_stop`；commit 成功但確認遺失時，閒置的 worker 補做，log `credential_rotation_restart_caught_up`），container restart policy 重啟，開機以 `load_account_credentials` 載入新憑證。重啟前仍用記憶體裡的舊 key（在 Bitfinex 端仍有效），沒有無 key 的空窗。
- operator 判斷：確認這次輪替是你送的；看到新的 bot run 開機成功後，才到 Bitfinex 刪除舊 key（key 衛生，不是交易步驟；完整步驟見 §11）。重啟後 Bitfinex 若拒絕新 key，bot 進憑證恢復模式（[`credential_recovery_mode`](#alert-credential_recovery_mode)），可以再送一次輪替（§11）。

<a id="alert-credential_rotation_rejected"></a>
#### `credential_rotation_rejected`（warning）

- 代表：輪替請求被拒，憑證沒有任何改動，`reason` 為：
  - 系統自己會恢復、重送即可：`recipient_key_stale`（請求封給的是已重啟的 bot run，CLI 會封給目前這次）、`deployment_in_progress`（最新的部署列是 15 分鐘內的 `started`，部署 settle 期間重啟會讓部署判失敗）、`bot_recently_started`（這次 bot 開機未滿 10 分鐘，部署可能仍在做 health 檢查與 settle；10 分鐘後重送）、`active_credential_changed`（送出後 active 憑證已換過）、`operator_not_authorized`（operator 的 membership 失效，先修好）。
  - operator 要判斷：`credential_account_mismatch`（新 key 屬於另一個 Bitfinex 帳號，確認建 key 的帳號）、`credential_reused`（這把 key 曾經用過並已 retired／revoked，請建新 key）、`invalid_credentials`（Bitfinex 拒絕這把 key）、`funding_write_required`／`withdraw_must_be_disabled`／`unexpected_write_scope:*`（調整 key 權限：只開 funding 寫入）、`account_identity_unavailable`（無法讀到 key 所屬帳號，確認 key 有 account info 讀取權限）、`sealed_payload_invalid`（密文損毀或被搬動，重送）。
- 系統會做：不碰憑證，請求就此結案，bot 不重啟。
- operator 判斷：依上面分類處理後重新送出（§11）。

<a id="alert-credential_rotation_failed"></a>
#### `credential_rotation_failed`（critical）

- 代表：輪替時發生錯誤；`reason` 為 `prepare_failed:BitfinexAPIError`（驗證 key 時 Bitfinex 沒有回答：連線、429、rate limit／維護代碼，或沒有 `["error", …]` 內容的 5xx；帶錯誤代碼的 500 例如 `apikey: digest invalid` 是拒絕，記成 rejected）、`prepare_failed:CredentialStoreError` 或 `credential_rotation_failed:<DBAPI 錯誤型別>`（資料庫錯誤，只記型別、不帶參數）、`credential_rotation_failed:<root exception>` 或 `outcome_write_failed:…`。
- 系統會做：rollback，舊憑證仍是 active，請求標為 failed、不重試，bot 不重啟。
- operator 判斷：暫時性錯誤時稍後重送；其他原因看 `docker logs bfx-bot` 的 `credential_rotation_failed`，交給開發處理。重送照 §11。

<a id="alert-credential_recovery_mode"></a>
#### `credential_recovery_mode`（critical）

- 代表：開機時 Bitfinex 拒絕帳戶的 API key（回應 `["error",10100,"apikey: …"]`，或 HTTP 401／403；`reason=venue_refused_key`），或 key 不合規（`reason=credential_noncompliant:<原因>`：`withdraw_must_be_disabled`、`funding_write_required`、`unexpected_write_scope:<scope>` 是權限不是只有 funding 可寫，`credential_account_mismatch` 是 key 屬於另一個 Bitfinex 帳號，`account_identity_unavailable` 是憑證列沒有存 user id；Bitfinex 的回答解析不了不算不合規，照沒回答重試），bot 進入憑證恢復模式。key 的權限與帳號在開機時檢查，執行中每 6 小時再查一次；執行中查到不合規時 bot 先以 exit 78 結束（`daemon_fatal`，`error=CredentialNoncompliantError: …`），重啟後的開機才發這則。例如 key 被撤銷或刪除、key 與 secret 對調。執行中被拒（reconcile 的讀取或送單；只認 body 帶 10100，不帶 body 的 401／403 可能是閘道頁面，照一般抓取失敗累計）時 bot 先以 exit 78 結束（`daemon_fatal`，`error=VenueCredentialRejectedError`），重啟後的開機才發這則。每個帳戶一則（dedup 欄位 `account`）。
- 系統會做：被拒期間不送單、不撤單、不寫帳本（重試的開機觀測成功那一次照常寫入觀測）；只跑換憑證 worker（照常發佈收件公鑰，CLI 可以封裝新 key 送進來）、writer lock、`health_check`、`db_keepalive` 與 healthz。`/healthz` 回 200（部署進恢復模式算部署成功，bfx-deploy 不會停掉 bot），`/readyz` 回 503。kill 等交易控制請求留在佇列，下次以可用的 key 開機後才套用並撤單。在 process 內每 1、2、4 分鐘、之後每 5 分鐘重試一次開機觀測；以下任一發生時乾淨重啟（`bot_runs` 記 `clean_stop`、exit 0），重啟後正常開機：輪替 applied、重試成功（key 在 Bitfinex 端恢復）、DB 裡的 active 憑證已換成另一把 key。重啟一律等這次 bot 開機滿 10 分鐘（部署 settle 期間不重啟）。其他錯誤（nonce 10114、invariant）照舊拒絕開機（`boot_refused`）。送單被拒而結束的那一筆送單記成 UNKNOWN（該幣別暫停），換上可用的 key 開機後由觀測自動結案（`not_accepted`），不需要裁決。
- operator 判斷：先判斷 key 是不是外洩或被人改動（Bitfinex 的 API key 頁面、帳號安全紀錄）；`credential_noncompliant` 時 key 的權限或帳號被改過，視同外洩處理。到 Bitfinex 產生一把新 key（只開 funding 寫入，IP 白名單照舊），以 `sudo bfx-op rotate-credential` 送出（§11）。bot 開機未滿 10 分鐘或部署進行中時請求會被拒（`bot_recently_started`／`deployment_in_progress`，見 [`credential_rotation_rejected`](#alert-credential_rotation_rejected)），屆時重送。輪替比對的是舊憑證列存下的 Bitfinex user id，所以舊 key 已撤銷也能換；active 憑證列一定存有 id（migration `36e59526f8cf` 的 CHECK），被換掉的列沒有 id 只可能是它在 id 寫入前就已 retired，此時回 `account_identity_unavailable`，以現在的 active 憑證重送（§11）。

<a id="alert-credential_recheck_failing"></a>
#### `credential_recheck_failing`（warning；`recovered=true` 時 info）

- 代表：執行中每 6 小時的 key 重查連續 12 次（第一次失敗後約 55 分鐘，每 5 分鐘重試一次）沒有得到結論：Bitfinex 沒回答、回答解析不了、拒絕的原因不是 key（例如 nonce 10114），或 DB 裡的 active 憑證已不是這把 key（`error=credential_superseded:replaced_by_rotation`）。完全沒有 active 憑證不在此列：重查判它不合規、exit 78，但重啟時讀不到憑證，開機被拒（`boot_refused`，每次重啟一則），不進恢復模式、不交易；只有人手改 DB 會造成這個狀態，恢復方式是把原本的憑證列改回 `active`（operator bootstrap 在帳號已有任何憑證列時會拒絕，不能用來重建）。`error` 只有例外型別，venue 原文在 `docker logs bfx-bot` 的 `credential_recheck_failed`。每段連續失敗只發一則；之後重查通過時發一則 `recovered=true`（info，附 `after` 次數），重新計數。
- 系統會做：照常放貸，每 5 分鐘再查一次；不停單、不結束 process。這段期間 key 的權限與帳號沒有被驗證（key 被改成可提領也要等重查恢復才會發現）。key 被輪替換掉時，正常情況下換憑證 worker 在幾秒內重啟 bot，不會累積到這則。
- operator 判斷：看 `docker logs bfx-bot` 的 `credential_recheck_failed`／`credential_recheck_key_superseded`。Bitfinex 維護或連線問題通常自己恢復；同一個 venue 錯誤代碼反覆出現（例如 10114）是 bug，交給開發。`credential_superseded` 持續出現代表輪替後的重啟沒發生，看 [`credential_rotation_applied`](#alert-credential_rotation_applied) 與 bot 的 `credential_rotation_restart` log。同時到 Bitfinex 的 API key 頁面確認這把 key 仍只開 funding 寫入。

<a id="alert-venue_observation_history_incomplete"></a>
#### `venue_observation_history_incomplete`（warning）

- 代表：某次 venue 觀測裡，某幣別的 history（`stream` 為 `offers`、`credits`、`loans`、`trades`，或消失 offer 的點查 `offers_by_id`）不完整；`reason` 為 `coverage_incomplete`（頁數上限 5 頁或 request budget 用完）、一筆解析不了的列（例外文字），或 120 秒 grace 內點查沒有答案（`not_returned`、`request_failed`、`cap_exhausted`、`unparseable`）。
- 系統會做：這次 observation 不被接受（`incomplete_or_unequal`），這個 cycle 不 deploy，UNKNOWN 也不能靠這份 history 結案；下一個 cycle 重試。`offers_by_id` 超過 grace 後改走 `offer_end_unconfirmed`。
- operator 判斷：偶發可以忽略；持續出現會接著收到 `reconcile_not_accepted`。`reason` 是解析錯誤時，可能是 venue 回傳格式改了，要交給開發處理。

<a id="alert-ledger_unknown_resolution_rejected"></a>
#### `ledger_unknown_resolution_rejected`（warning）

- 代表：observation 被接受後，自動結案判定要綁定或標記某筆 UNKNOWN，但 journal 拒絕寫入（`ResolutionRejected`，例如 attempt 已不是 UNKNOWN、scope 不符；或 `ResolutionAlreadyRecorded`，已經有人先結案）。附 attempt_id、symbol、action、error。
- 系統會做：只 rollback 這一筆結案，UNKNOWN 維持開啟、該幣別維持隔離；其他結案照常寫入，下一個被接受的 observation 再試。
- operator 判斷：`error` 是已經結案時，多半是你剛處理過，確認它已結案即可。其他情況照 §5 處理；反覆出現要回報成 bug。30 分鐘後仍未結案會接著收到 `unknown_quarantine_aged`。

<a id="alert-venue_submit_rejected"></a>
#### `venue_submit_rejected`（warning）

- 代表：deployment 送單被 venue 明確拒絕（HTTP 4xx／200 的結構化拒絕，或 HTTP 500 帶 `VENUE_SUBMIT_REFUSALS_5XX` 列出的訊息，例如 deposit wallet 餘額不足）。附 symbol、cell、reason（含 venue 的 code 與訊息）。同一個 symbol 與 reason 10 分鐘內只發一次。
- 系統會做：這筆 attempt 記成 rejected，reservation 釋放，幣別不隔離；下一個 tick 依最新的 ledger 重新 sizing 再送。
- operator 判斷：偶發一次（例如 credit 剛回到錢包的空檔）可以忽略。每 10 分鐘反覆出現表示 bot 的可用額估計一直高於 venue 的認定，要交給開發查 ledger 與 venue 錢包的差異。

## 7. 改了 runtime env 之後重建 container

bfx-deploy 只在有新 release（或 `--retry`）時自動重建 container；只改 `/opt/bfx/runtime/*.env`
或 `/opt/bfx/runtime/secrets/` 不會觸發。換秘密見 [secret-rotation](secret-rotation.md)。改完後：

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
| `bfx-deploy.timer` | 每 5 分鐘（`*:2/5`） | 部署最新綠燈 main；沒有要部署時修復卡在 holder 死 namespace 的加入者（[deploy §5](deploy.md#加入者留在-holder-已死的-network-namespace)；要立刻重試就刪 `/var/lib/bfx-deploy/netns-repair.json`） |
| `bfx-pgbackrest-backup.timer` / `bfx-pgbackrest-status.timer` | 見 unit | 備份與 RPO evidence（`OnFailure=bfx-alert@`） |
| `bfx-backup-check.timer` | 每 5 分鐘（`*:1/5`） | evidence、restore heartbeat、weekly report heartbeat 與磁碟使用率檢查，連續兩次才告警，6 小時重發（見下方「主機檢查」） |
| `bfx-restore-test.timer` | 每月 1 日 09:17 UTC | `bfx-restore-test@current`：用已部署 release 的 DR 腳本做 isolated restore ＋ ledger 驗證（與 production 比對有界 digest、唯讀 boot check；[offsite DR](offsite-dr.md#monthly-and-change-triggered-ledger-restore-test)） |
| `bfx-weekly-report.timer` | 每週一 04:17 UTC | 每週量測鏈：attribution／realized interest／G3 報告＋研究重驗（見下方「每週報告」；`OnFailure=bfx-alert@`） |

`systemctl list-timers 'bfx-*'` 看實際啟用狀態。

### 人工定期事項

| 事項 | 期限 | 做法 |
|---|---|---|
| 換 `/opt/bfx/runtime/attest.env` 的 GitHub token（驗 provenance 用，效期 1 年） | 到期日前至少一週；到期日：`<建立時填 YYYY-MM-DD>` | [deploy runbook「token 到期與每年輪替」](deploy.md#attestenv-的-token-到期與每年輪替)；過期後預期新 release 會以 `attestation_unverified` 停在 discovery（未實測），換 token 前不會恢復 |

### 每週報告（`bfx-weekly-report`）

步驟定義在 `deploy/vm/ops/docker-compose.weekly-report.yml`，和主機工具一起由 bfx-deploy 在每次
成功部署後安裝到 `/usr/local/lib/bfx-ops/releases/<rev>/ops`（`current` 指向它）；
`bfx-weekly-report.service` 在 `managed-units` 裡，同時被安裝與 `daemon-reload`。
VM 上 `/home/ubuntu/bfx-funding-bot` 的 working tree **不再被讀取**（它不會被更新）。

- **Image**：ledger 最新 `deployed` 列的 backend digest（`<repository>@sha256:…`，與 production
  跑的是同一個），不用 `bfx-bot:local`。步驟來自已安裝的工具版本；若兩者 revision 不同
  （部署後工具安裝失敗），照跑並在 journal 印 warning。
- **Env**：只傳三個值——`DATABASE_URL`、`BFX_EXCHANGE_ACCOUNT_ID` 取自 `/opt/bfx/runtime/research.env`
  （研究 role `bfx_research`：只能寫研究表，不能寫 ledger 或 `trading_state`；
  [fresh-host-setup §1](fresh-host-setup.md#1-database-roles-與-grants)），`BFX_DEPLOYMENT_ENV` 取自該部署 revision 的 `live.env`
  （`/var/lib/bfx-deploy/releases/<rev>/live.env`）。runner 不讀 `bot.env`；`research.env` 不存在或不是
  root 0600 時拒跑（`cannot run: secret_file_missing:research.env`／`secret_file_mode_not_0600:research.env`），不會退回 `bot.env`。
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

成功（exit 0）的一次會寫 `/var/lib/bfx-ops/weekly-report-heartbeat.json`；timer 啟用時，
`bfx-backup-check` 在它超過 8 天時發 warning。新主機在第一次成功之前沒有 heartbeat，不告警。

### 主機檢查（`bfx-backup-check`）

| 問題碼 | 級別 | 條件 |
|---|---|---|
| `rpo_exceeded`、`backup_evidence_*`、`backup_unmeasured` | critical | 見 [offsite DR](offsite-dr.md) |
| `restore_test_no_heartbeat` | critical | restore timer 啟用但沒有 heartbeat |
| `restore_test_stale` | critical | restore heartbeat 超過 35 天（不論 timer 是否啟用） |
| `restore_timer_disabled` | warning | heartbeat 存在（這台跑過 restore test）但 timer 停用 |
| `weekly_report_stale` | warning | weekly timer 啟用且最後一次成功超過 8 天 |
| `disk_usage_high`／`disk_usage_critical` | warning／critical | `/` 使用率 ≥85%／≥95%（pgBackRest repo 與 Docker volume 都在這裡） |

- 系統自己會做的：只通知，不擋部署、不改交易狀態；同一問題連續兩次才告警，6 小時重發，清除時發一則 recovered。
- operator 要判斷的：磁碟告警先看 `docker system df`（舊 image 與 build cache 通常可回收）與
  pgBackRest repo 是否因 R2 斷線而累積（[offsite DR](offsite-dr.md)）；restore timer 停用時判斷是刻意暫停還是漏開。
- 試發：`sudo /usr/local/lib/bfx-ops/current/ops/.venv/bin/python /usr/local/lib/bfx-ops/current/ops/bfx_backup_check.py --state /tmp/backup-check-test.json --disk-warn-percent 1`
  連跑兩次，第二次會送出 warning（用暫存 state，不影響正式的去重狀態）。

## 9. 同一把 API key 的其他使用者（nonce 衝突）

Bitfinex 對**每把 API key** 只接受遞增的 nonce，並以**抵達順序**判斷：比最後收到的還小就回
HTTP 500（body 為 `nonce: small`）。bot 內所有簽章請求共用一個 `AuthRequestGate`
（`external/bitfinex/nonce.py`），依序送出，下單／撤單／kill 優先於背景讀取；但這個 gate
**只管 bot 這一個 process**。以下用同一把 key 的呼叫不經過它，可能讓 bot 的請求（包括撤單或
kill 的 cancel-all）被拒：

- 一次性腳本：`scripts/bootstrap_account_credential.py` 等會打 Bitfinex auth 端點的工具
- 任何在其他機器上用同一把 key 的手動測試

做法：

- **交易進行中不要跑這些**；需要時先停止帳戶（§3）或在沒有下單活動時執行，跑完看 bot log 有無
  `bitfinex_auth_http_error ... nonce`。
- 需要常態的外部讀取（驗證、報表、除錯）時，改用**另一把只有讀權限的 key**：nonce 是按 key
  計算，不同 key 互不影響，唯讀也避免誤下單。
- bot log 出現 `bitfinex_auth_gate_slow_wait`（等 gate 超過 2 秒，含等待者種類）表示 venue 變慢或
  讀取堆積；背景讀取每筆最多 10 秒就會被切斷。

## 10. Operator CLI（`bfx-op`）

高風險操作（恢復交易、啟用幣別、UNKNOWN 裁決、換交易所憑證）只走這條路（ADR 2026-10-15 D4）。

- **入口**：從自己的裝置 `tailscale ssh ubuntu@oci-a1`（或連到 VM tailnet IP 的 `ssh`，同樣經過
  Tailscale SSH），通過 check mode 的 IdP 驗證（12 小時內同一台裝置不再問），然後
  `sudo bfx-op <子命令> …`。不要用 `docker compose run` 直接跑 operator service。
- **它做什麼**：取 session 的 peer（`SSH_CONNECTION` 第一欄，必須是 tailnet 位址）→
  `tailscale whois` 取得你的 tailnet user ID → 以 ledger 最新 `deployed` 的 backend image 跑一次
  `docker compose -p bfx-operator … run --rm -T operator <子命令>`。這個 user ID 就是請求的
  `requested_by`（9d-6 起）；沒有指定 actor 的參數。CLI 連上 DB 後先確認它是該帳戶的 member，
  不是就拒絕、什麼都不寫；寫入另要 owner／operator，bot 套用時再以 `operator_authorized` 重驗。
- **信任邊界**：`ubuntu` 的 sudo 是 NOPASSWD，任何 root 等價的 shell 都能自己設定
  `SSH_CONNECTION` 冒充另一個 peer：公網 sshd 還開著時持有 key 的登入、Console 上的 `ubuntu`。
  公網 sshd 停用後只剩 Console 這一條。誰能開 session 由 Tailscale SSH 的 check mode 決定；`bfx-op`
  做的是把誠實 session 的身分綁進請求並留下紀錄。
- **子命令**（`sudo bfx-op <子命令> --help` 看參數；`halt` 除外，它只收 `--reason`）：唯讀 `status`、`uncertainties`、`uncertainty`、
  `request <trading|currency|resolution|credential> <request_id>`、`credential`；寫入 `resume`、`kill`、
  `enable`、`disable`（§2、§3）、`bind-to-venue`、`mark-not-accepted`、`manual-resolution`（§5）、
  `rotate-credential`（§11）；緊急停機 `halt`（§3，不要求 tailnet 身分、不經 operator container）。寫入預設等到結果（`--wait-seconds`，預設 120）；`--no-wait` 只排入。
  `sudo bfx-op --dry-run <子命令> …` 只印出會跑的 image、身分與命令。
- **輸出**：stdout 一個 JSON；寫入時 stderr 一行 `operator_request request_id=… action=…`。

| exit | 意義 |
|---|---|
| 0 | 讀取完成、請求已套用，或 `--no-wait` 已排入 |
| 1 | 內部錯誤，或 `bfx-op` 組不出要跑的東西（`cannot run: <code>`，例如 `operator.env` 不是 root 0600、ledger 沒有 `deployed` 列） |
| 2 | 參數錯誤；子命令前只接受 `--dry-run`；沒有用 sudo |
| 3 | 被命令檢查或 bot 拒絕（輸出的 `error`） |
| 4 | bot 套用失敗 |
| 5 | 等到 `--wait-seconds` 仍未結案（請求仍在佇列，`request` 查結果） |
| 77 | 沒有 tailnet 身分（`not_tailnet_peer`、`tailnet_whois_failed`、`operator_identity_invalid`）或不是該帳戶的 member（`operator_not_member`；`operator.env` 的 `BFX_EXCHANGE_ACCOUNT_ID` 指到不存在或已 retired 的帳戶也是這個碼，兩者刻意不分） |
| 78 | schema head 或資金權威不是這個 image 的 |

- **紀錄**：`sudo journalctl -t bfx-op --since today --no-pager`。每次呼叫一行 `start`（login、tailnet user
  ID、裝置、peer、子命令、revision）與一行 `end`（request id、action、exit）；拒絕時一行 `refused`。
  `halt` 的兩行改帶 `identity=tailnet|none` 與 `actor`。
  不記其他參數，也不記 stdin。

## 11. 換交易所憑證

前提：

- 在 Bitfinex 建一把新 key，權限與現行相同：funding 可寫、withdraw 關閉、其他唯讀（含 account info）。
- bot 這次開機已滿 10 分鐘，而且沒有部署正在進行（否則會被拒 `bot_recently_started`／`deployment_in_progress`，稍後重送即可）。

步驟：

1. 在自己的 Tailscale SSH session 執行，於提示輸入 key 與 secret（不回顯）。不要從檔案或 shell 變數 pipe 進去：
   ```bash
   sudo bfx-op rotate-credential --reason "為什麼換" --label "YYYY-MM-DD"
   ```
   key 與 secret 只在 `bfx-op` 與 CLI 的記憶體裡，以這次開機的 bot 的公鑰加密後才排入；不進 journal、log 或參數。
2. 結果 `applied` 後 bot 自動重啟（[`credential_rotation_applied`](#alert-credential_rotation_applied)）。確認：
   - `sudo bfx-op credential` 的 active 憑證是結果裡的新 id；
   - `sudo docker inspect -f '{{.State.StartedAt}}' bfx-bot` 晚於 applied 的時間；
   - `sudo bfx-op status` 正常，Telegram 沒有 `boot_refused`。
3. 以上都成立後，才到 Bitfinex 刪除舊 key（key 衛生，不是交易步驟）。刪之前舊 key 一直可用，新 key 有問題時 bot 仍有退路。

被拒或失敗：看 §6 的 [`credential_rotation_rejected`](#alert-credential_rotation_rejected)／[`credential_rotation_failed`](#alert-credential_rotation_failed)，拒絕時憑證沒有任何改動。
現行 key 已經被 Bitfinex 拒絕（例如撤銷或刪除）：bot 在憑證恢復模式（[`credential_recovery_mode`](#alert-credential_recovery_mode)），換憑證 worker 照常執行，照上面的步驟送出即可；等 bot 開機滿 10 分鐘再送。比對帳號用的是舊憑證列存下的 Bitfinex user id，不需要舊 key 可用；active 憑證列一定存有 id（migration `36e59526f8cf` 的 CHECK）。換憑證只有這一條路：憑證表的 INSERT／UPDATE 與寫入端點都已自 webapi 移除。

## 12. Break-glass

順序：Tailscale SSH（§10）→ OCI Console Connection。終態是公網 sshd 停用、UFW 與 OCI security list 都不開 22；
在那之前公網 sshd 仍是一條入口（`sudo systemctl is-active ssh.socket ssh.service` 看現況）。

| 元件壞了 | web UI | `bfx-op`（Tailscale SSH） | Console Connection |
|---|---|---|---|
| webapi、tailnet 入口（`bfx-netns`／`bfx-tailscale`） | 不可用 | 可用 | 可用 |
| Tailscale control plane、IdP | 不可用 | 不可用 | 可用 |
| host tailscaled | 可用（入口節點在自己的容器裡） | 不可用 | 可用 |
| Postgres、docker | 不可用 | 不可用 | 可用（只做主機修復） |

「web UI」欄只指 kill、停用幣別與各種讀取；resume、啟用幣別、UNKNOWN 裁決與換憑證不論哪個元件壞了都只走 `bfx-op`
（ADR 2026-10-15 D4），web UI 可用時也一樣。

Tailscale 不通時：

1. 從 OCI Console 開 instance 的 Console Connection（OCI 只接受 RSA 公鑰，不接受 ed25519），以 `ubuntu` 與
   break-glass 密碼登入。
2. 查 `tailscale status`。node key 過期就 `sudo tailscale up` 重新驗證；tailscaled 異常就
   `sudo systemctl restart tailscaled`。需要時可暫時重新啟用 sshd：
   `sudo systemctl unmask ssh.socket ssh.service && sudo systemctl enable --now ssh.socket`、
   `sudo ufw allow 22/tcp`，並在 OCI security list 加回 22；修好後照原樣停用、刪掉 UFW 規則、移除 OCI 的 22。
3. Console 下 `bfx-op` 除了 `halt` 一律拒絕（`not_tailnet_peer`）：高風險操作只走 Tailscale SSH。這段期間 bot
   照常自動放貸；要降低曝險，用 §3 的 `sudo bfx-op halt --reason "…"`，或停掉 bot container（§7）。

