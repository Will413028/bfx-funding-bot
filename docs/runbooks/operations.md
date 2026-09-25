# Operations runbook（交易狀態、核准、停機、告警）

日常操作。部署見 [deploy runbook](deploy.md)；備份與還原見 [offsite DR](offsite-dr.md)；
行為的權威描述在 `backend/ARCHITECTURE.md` §6（Trading state、自動保護、Release flow、
限額期、Kill switch）。決策來源：ADR 2026-09-25 `automated-probation-replaces-release-ceremony`（D1–D6）。

## 1. 交易狀態

`trading_state` 是「能不能掛新單」的唯一權威：append-only，每列有 state、cause、actor、reason。

| state | 新單 | 撤單 | 進入時 |
|---|---|---|---|
| `ACTIVE` | 可（限額期中受限額） | 可 | — |
| `REDUCING` | 不可、也不重掛 | 可 | 無 venue 動作 |
| `HALTED` | 不可 | 可 | 先 commit HALTED，再對每個幣別呼叫 venue funding cancel-all（含 orphan 與有 UNKNOWN 的幣別） |

cause：`operator`（人）、`kill_switch`（`BFX_KILL_SWITCH`）、`auto`（自動保護）、
`material_deploy`（material 部署等待核准）。讀不到狀態或從未記錄任何決策 ＝ HALTED（fail-closed）。

非法轉換由 DB trigger 與程式碼雙重拒絕：HALTED→REDUCING、非 operator 把 REDUCING/HALTED 改回
ACTIVE（唯一例外是限額期通過時的 `ACTIVE/auto`）、把 material 部署的 REDUCING 改標成 operator pause。

## 2. 操作入口

### 主要：UI（TOTP）

Overview 的「交易狀態」面板。所有請求都經 frontend BFF 的 MFA 閘門（已驗證 TOTP 的 operator
session），webapi 只把請求排入 `trading_control_requests`（回 202），由 bot 的
`TradingControlWorker` 在 account lock 下重新驗證 operator 後套用，結果（applied／rejected＋原因／
failed）顯示在面板上。

| 按鈕 | 效果 |
|---|---|
| 核准這個 build | 只在 material build、且該 digest 未核准時出現。`REDUCING/material_deploy` → `ACTIVE` 限額期；若當下是 HALTED/其他 REDUCING，只記錄核准、狀態不變（之後 resume 才進限額期） |
| 恢復交易 | `REDUCING`/`HALTED` → `ACTIVE`。material build 未核准會被拒（`approval_required`）；自動停機之後、從未記錄決策、或這個 material build 尚未通過限額期 → 進新的限額期；限額期未通過時的 pause/operator 停機 → 同一組限額重新計 24 小時 |
| 暫停（只准撤單） | `REDUCING/operator`，無 venue 動作 |
| Kill switch…（輸入 `KILL`） | `HALTED/operator` ＋ venue cancel-all；同時把其他等待中的請求標成 `superseded_by_kill`。kill 有自己的佇列、優先處理，永遠不會因其他請求而被擋。已經是 HALTED 時再按一次 ＝ 重試 cancel-all |

核准與恢復的請求帶著面板上看到的 backend digest；不是正在跑的 build 就被拒（`digest_not_running`）。
面板的「本次停機的 venue 撤單」顯示 `funding_cancel_all_audit` 的結果；顯示不完整時，重試 kill
或到 Bitfinex 手動撤單。

**恢復只有 UI 這一條路。** 舊的 `/admin/resume` 已刪除；static token 不能恢復或核准。

### 緊急備用：static admin token（只能降低曝險）

UI 不可用（frontend/webapi/auth 掛掉）時使用。bot 的 healthz server（container 內 port 8080，
未對 host 公開）提供：

- `POST /admin/halt?reason=...&actor=...`：與 UI kill 同一條 kill path。200＝HALTED 且每個幣別
  cancel-all 都 acknowledged；502＝HALTED 已生效但 venue 部分未完成，再呼叫一次即重試。
- `POST /admin/pause?reason=...&actor=...`：`REDUCING/operator`，無 venue 動作。

在 VM 上執行（token 從 container 自己的環境讀，不印出）：

```bash
sudo docker exec bfx-bot /app/.venv/bin/python -c '
import os, sys, urllib.error, urllib.parse, urllib.request
q = urllib.parse.urlencode({"reason": sys.argv[2], "actor": "operator-cli"})
r = urllib.request.Request("http://127.0.0.1:8080/admin/" + sys.argv[1] + "?" + q, method="POST",
    headers={"Authorization": "Bearer " + os.environ["BFX_ADMIN_TOKEN"]})
try:
    print(urllib.request.urlopen(r, timeout=120).read().decode())
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode()); sys.exit(1)
' halt "reason text"          # 或 pause
```

恢復仍需 UI（TOTP）。bot 自己停著時兩者都不可用：到 Bitfinex 網頁撤單並記錄。

`BFX_KILL_SWITCH=true`（bot.env）是 DB 故障時的 break-glass：guard 直接擋新單，開機時也走 kill
path。改 bot.env 需要重建 bot container（見 §7）。

## 3. Release flow：核准與限額期

- standard release：保持部署前的交易狀態，不需任何操作。
- material release：bot 開機時若該 digest 沒有核准 → `REDUCING/material_deploy`（原本 HALTED
  就維持 HALTED），Telegram 通知 `deploy_gate`。到 UI 核准一次。
- 缺少或格式錯誤的部署身分一律視為 material（例如不是由 bfx-deploy 啟動的 container）。

**限額期（probation）**：新單的 cell 上限 ＝ min(正常值, max(正常值×25%, 開始時 venue 的一筆最小單))。
滿 24 小時、期間 ≥3 筆 acknowledged submit、且期間沒有 HALTED 時，worker 自動寫 `ACTIVE/auto`
解除（這是唯一能結束限額期的轉換）。面板顯示進度（小時、ack 數、各幣別最小單）。

開始新限額期：material 核准、`HALTED/auto` 之後的 resume、從未記錄決策的第一次 resume、以及核准時
仍在停機的 material build 的第一次 resume。維運 pause（不在限額期內）恢復時不帶限額。

Telegram 會收到 `probation_started`、`probation_lifted`、`trading_control_applied|rejected|failed`
與每一次交易狀態轉換。

## 4. 自動保護（寫 `HALTED/auto` ＋ kill path，不會自動解除）

| trigger | 意思 |
|---|---|
| `submit_outcome_unknown` | 送單結果不明（或 recovery 把中斷的 PENDING 轉成 UNKNOWN） |
| `orphan_quarantined` / `unattributed_offer` | venue 上有沒有本地來源的掛單 |
| `unclassifiable_commitment` | 某筆 commitment 無法放進資金快照 |
| `offer_amount_mismatch` | 受管 offer 的 venue 金額與送出金額不同 |
| `identity_conflict` | ledger 與 venue、或兩筆 ledger 對同一 commitment 的身分不一致 |
| `venue_lent_above_ledger` | venue 借出額高於 ledger＋我方成交能解釋的量（>0.01） |
| `loss_limiter` | 某幣別 24h 虧損或 drawdown 超限 |
| `writer_lock_lost` | recovery 後仍未持有 writer lock |
| `command_rate_exceeded` | command gate 持續 throttle venue 寫入（T9） |

**不會**觸發：借款到期造成 lent 減少、reconcile 補回 WS 漏掉的成交或撤單。

已經 HALTED 時再次觸發只記錄，不重跑 cancel-all（operator 的 kill 則一定重跑）。

處理步驟：

1. 看 Telegram 與 UI 的 cause/reason；`journalctl` 不適用（container log 用
   `docker logs bfx-bot --since 1h`）。
2. 確認面板的 venue cancel-all 結果；不完整就重試 kill。
3. 找出原因並處理 uncertainty（§5）；`venue_lent_above_ledger`、`identity_conflict` 等需要先查清楚
   ledger 與 venue 的差異。
4. 原因消除後在 UI「恢復交易」（TOTP）→ 自動進入 24 小時限額期。

## 5. UNKNOWN 與 orphan 的人工處理

Overview 的 uncertainty 明細提供三種請求（同樣經 MFA、排入 `uncertainty_resolution_requests`，
由 bot 套用；需要新鮮的 reconcile fence，過期會被拒 `stale_reconcile_fence`）：

| uncertainty kind | 可用動作 |
|---|---|
| `submit_outcome_unknown`（UNKNOWN 送單） | `bind-to-venue`：綁到 venue 上唯一且完全符合的 offer；`mark-not-accepted`：有完整 venue history 證明 venue 沒收下 |
| `unattributed_venue_offer`（orphan）、`unsupported_venue_exposure` | `manual-resolution`：`closed_at_venue`（已在 venue 關閉，例如被 cancel-all 撤掉）或 `accepted_external_exposure`（確認是外部曝險、接受它） |

規則：

- 被 cancel-all 撤掉的 UNKNOWN offer 會由 offer-history 比對自動解析；orphan 永遠需要人工 resolution。
- 零個或多個候選 ＝ 維持 UNKNOWN，不要猜。不要刪資料列、不要自己合成 venue reference。
- 需要 DB 還原或 venue 寫入之後的回滾判斷時，改走
  [rollback after a venue write](rollback-after-venue-write.md)。

## 6. 告警

兩條 Telegram 路徑，**用同一組 bot token 與 chat id**：

| 來源 | 設定檔 | 內容 |
|---|---|---|
| bot 內的 sink（`observability/alerts.py`，非阻塞） | `/opt/bfx/runtime/bot.env` 的 `TELEGRAM_BOT_TOKEN`／`TELEGRAM_CHAT_ID` | 交易狀態轉換、自動保護、UNKNOWN、orphan、kill 結果、deploy gate、核准/恢復結果、限額期開始/解除、`venue_offers_may_remain` |
| host 工具 `bfx-notify` | `/opt/bfx/runtime/notify.env`（同樣兩個 key） | bfx-deploy 結果、bfx-backup-check（RPO>300s、evidence 超過 15 分鐘、restore heartbeat 超過 35 天）、`bfx-alert@` 的 unit 失敗（pgBackRest 備份、restore test） |

- 兩個都空 → bot 只記 log（開機時會說一次）；`bfx-notify` 永遠 exit 0，送不出去只記 journal。
- **換 token 或 chat id 要兩個檔都改**：改 `notify.env` 立即生效；改 `bot.env` 要重建 bot（§7）。
- 測試：`sudo bfx-notify --level info "test"`。
- Grafana 仍是第二條觀測路徑。

## 7. 改了 runtime env 之後重建 container

bfx-deploy 只在有新 release（或 `--retry`）時重建 container；只改 `/opt/bfx/runtime/*.env`
不會觸發。以 ledger 最新一筆 `deployed` 的值手動重建（值要完全照抄，特別是 `change_class`：
把 standard build 標成 material 會讓 bot 進 `REDUCING/material_deploy`）：

```bash
docker exec --user postgres bfx-postgres psql -U bfx -d bfx -At -F ' ' -c \
  "SELECT source_revision, backend_digest, frontend_digest, change_class
     FROM deployments WHERE outcome='deployed' ORDER BY id DESC LIMIT 1"

REV=<source_revision>; BD=<backend_digest>; FD=<frontend_digest>; CLASS=<change_class>
sudo env BFX_BACKEND_IMAGE=ghcr.io/will413028/bfx-funding-bot-backend@$BD BFX_BACKEND_DIGEST=$BD \
  BFX_FRONTEND_IMAGE=ghcr.io/will413028/bfx-funding-bot-frontend@$FD BFX_FRONTEND_DIGEST=$FD \
  BFX_SOURCE_REVISION=$REV BFX_CHANGE_CLASS=$CLASS \
  docker compose -p bfx-app -f /var/lib/bfx-deploy/releases/$REV/docker-compose.app.yml \
  up -d --no-deps --force-recreate bot       # 或 webapi / frontend
```

先 `sudo systemctl stop bfx-deploy.timer`，完成並確認 `/healthz` 與 UI 正常後再 start。
這條路徑不經 bfx-deploy 的 hardening 再檢查與健康等待，也不寫 ledger；只用於 env 變更。

## 8. 定期與背景工作

| unit | 排程 | 作用 |
|---|---|---|
| `bfx-deploy.timer` | 每 5 分鐘（`*:2/5`） | 部署最新綠燈 main |
| `bfx-pgbackrest-backup.timer` / `bfx-pgbackrest-status.timer` | 見 unit | 備份與 RPO evidence（`OnFailure=bfx-alert@`） |
| `bfx-backup-check.timer` | 每 5 分鐘（`*:1/5`） | evidence 與 restore heartbeat 檢查，連續兩次才告警，6 小時重發 |
| `bfx-restore-test.timer` | 每月 1 日 09:17 UTC | isolated restore ＋ prefix-hash 驗證（[offsite DR](offsite-dr.md#monthly-and-change-triggered-prefix-restore-test)） |
| `bfx-weekly-report.timer` | 每週 | attribution／G3 報告（用 `bfx-bot:local`，bfx-deploy 每次成功部署後重新 tag） |
| `bfx-halt-watch.timer` | 見 unit | 以 `/admin/dry-evaluate` 檢查停機是否真的生效 |

`systemctl list-timers 'bfx-*'` 看實際啟用狀態。
