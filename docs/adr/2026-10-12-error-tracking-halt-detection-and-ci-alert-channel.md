---
title: 錯誤追蹤用 Sentry SaaS、halt 偵測改用 metrics、排程檢查的失敗送同一個 Telegram bot
date: 2026-10-12
status: active
tags: [bfx-funding-bot, decision, observability, alerting, security]
related-commits:
  - 603c85ed
  - "92487caf^..bd7ae476"
  - "1a7f2438^..ae450002"
---

# 錯誤追蹤、halt 偵測與排程檢查的通知管道

## Context

2026-10-12 把 repo 補到 live 級（真錢、持有交易所憑證）時，盤點出三個偵測缺口：

- 沒有錯誤追蹤。後端沒有 SDK；前端裝了 `@sentry/nextjs` 卻沒有 DSN。原建議是在 Loki 寫字串規則。
- halt 時仍送單沒有獨立偵測。送單由 command gate 在應用層把關（admission 在鎖內查 guard、commit 後 transport recheck、kill 等待 in-flight）；事後偵測靠 `halt-watch` 腳本，它的四個訊號只有 `HALT FAILED` 不和其他告警重複，而且 VM 上的 timer 已 masked 一個月沒跑。
- 新的每日外部依賴檢查（GitHub Actions 打 Bitfinex 公開 endpoint 與公開站）失敗時要通知誰。

**約束**：

- `external` 單一 operator、只有 owner 自己的小額資金；所有告警由同一個人看。
- `external` Telegram bot token 不能縮權限：拿到 token 就能用整個 Bot API（讀 bot 收到的訊息、改刪 bot 發過的訊息）。
- `external` 自架服務若跟 bot 同一台 VM，會跟著一起掛。
- `inherited` 告警永不阻塞交易（`modules/observability/alerts.py` 有界 queue、丟棄不阻塞）：仍成立，交易路徑不能等網路。
- `inherited` 憑證與個資不送進外部服務（`alerts.py` 的 secret 遮罩）：仍成立，live 級的 credentials 風險要求。
- `inherited` bot 與主機告警都走 Telegram：仍成立，單一 operator 只看一個地方。

## Options Considered

### 錯誤追蹤

- **基準. error tracking 服務依 fingerprint 分組，只在新 issue／回歸時通知；page 類告警只看症狀**：Google SRE book ch.6〈Monitoring Distributed Systems〉、Prometheus〈Alerting〉best practices。
- **A. Loki 字串規則**：不需新服務；但同一錯誤每次都觸發、抓不到瀏覽器錯誤、把 log 原文送進告警。
- **B. Sentry 協定 SDK，用 Sentry SaaS（採用）**：bot、webapi、前端都接。
- **C. 同協定，自架 GlitchTip**：放在同一台 VM。

### halt 偵測

- **基準. 下單路徑上的事前控制在應用層，另有獨立的事後監測**：SEC Rule 15c3-5（market access 的 pre-trade risk controls 放在下單路徑）；事後偵測與執行路徑分開。
- **A. DB 層強制**：trigger 在 halted 時拒絕送單紀錄。
- **B. 不變量留在應用層，事後偵測改成 metrics（採用）**。
- **C. 修好 halt-watch 腳本繼續用**。

### 排程檢查的通知

- **A. GitHub 的失敗 email**：不需新 secret；只寄給最後改 cron 的人，與其他告警不同管道。
- **B. workflow 加 Telegram secret，送到同一個 chat（採用）**。
- **C. 改成 VM 上的 systemd timer，走 `bfx-notify`**：不需 CI secret；VM 掛掉時檢查也停。
- B 的子題：**B1. GitHub 與 VM 共用同一個 bot token（採用）**；**B2. 另開一個只給 CI 用的 bot**，送到同一個 chat，輪替互不影響。

## Decision

- **D1 = 計畫 D2**：選 B，Sentry SaaS。後端 `include_local_variables=False`、`send_default_pii=False`，送出前沿用 secret 遮罩並移除 email／IP，遮罩失敗就丟掉事件；DSN 未設就不初始化。前端 DSN 由 CI build 注入，瀏覽器事件經同源 tunnel `/fe-events` 轉送（內容攔截器會擋直連 `*.sentry.io`）。通知只在新 issue、回歸、頻率超過門檻；page 仍只看 Grafana 規則與 bot 的具名告警。
- **D2 = 計畫 D5**：選 B。bot 匯出 `bfx_trading_halted` gauge（值與 `/admin/trading-status` 同源，讀不到不更新）；Grafana 規則：`min(min_over_time(bfx_trading_halted[6m])) == 1 and on() sum(increase(bfx_executor_submits_total{status!="not_sent"}[5m])) > 0`、`for: 2m`、critical。退役 halt-watch 與從未安裝的 l3-verify。這是 [2026-07-27](2026-07-27-halt-control-and-behaviour-observability.md) Followup 原訂的終態。
- **D3 = 計畫 D8、D17 Q3**：選 B＋B1。三個地方（bot 秘密檔、主機 `notify.env`、GitHub repository secret）用同一個 token，輪替時一起換（`docs/runbooks/secret-rotation.md`「Telegram bot token」）。

## Rationale

- **D1**：A 會刷屏、看不到瀏覽器，還把 log 原文送出；把做法推離業界基準的理由都是自訂限制，不是外部約束。選 SaaS 而非 C：自架在同一台 VM 會和 bot 一起掛，正好在最需要時看不到錯誤；協定相同，日後換 DSN 就能搬到 GlitchTip。**代價**：錯誤事件離開主機到第三方，所以遮罩規則比 Telegram 更嚴，遮罩失敗寧可不送。
- **D2**：送單只有 command gate 一條寫入路徑，事前控制已在應用層強制，業界的 pre-trade control 也放在這裡；缺的是獨立的事後偵測，不是第二道強制，所以不選 A。不選 C：它唯一不重複的訊號用 metrics 規則就能表達，而腳本已經一個月沒跑都沒人發現。**代價**：規則用 `min_over_time` 與 `for: 2m` 避開 halt 前與恢復後的誤報，halt 後約一分鐘內的單筆送單不在偵測範圍。
- **D3**：選 B 而非 A、C：所有告警集中在 operator 已經在看的 chat；A 只寄給最後改 cron 的人；C 在 VM 掛掉時這條檢查也停（只剩 heartbeat 告警）。B1 而非 B2（2026-10-24 Will 決定維持共用）：**代價**是 GitHub secret 外洩等於 VM 告警 bot 外洩，拿到 token 的人能讀、改、刪 bot 的訊息；B2 能把兩者隔開，但要多管一個 bot 和一份輪替。原本「這個 secret 只能送訊息」的說法不成立（2026-10-24 設計審查更正），所以這是接受的風險，不是零風險。

## Result

- `git log --oneline -1 603c85ed`（PR #188：gauge、退役 halt-watch／l3-verify）；PR #191、#192（Sentry 後端與前端 tunnel）；PR #276（`venue-contract.yml`）。
- 部署後實測：bot、webapi、瀏覽器的測試事件都進 Sentry，email／IP 為 `[redacted]`、stack frame 沒有變數；Grafana halt 規則的 Test 與 backup-check 試發都送到 Telegram（2026-10-13）；`venue-contract` 的 mutation run 失敗、`notify` job 成功送出。
- 如預期。

## Invariants

- 錯誤追蹤與 metrics 都不在送單路徑上同步執行；Sentry 送出是 SDK 背景 thread，關機 flush 有上限。
- 沒遮罩成功的事件不送出（`modules/observability/error_tracking.py` 的 `scrub_event`，接在 SDK 的 `before_send`）。
- Grafana 規則依賴的 metric 名稱與 `status` 標籤值由 bot repo 的測試鎖住；改名要同步改 homelab 的規則。

## Revocation Triggers

- 有 issue 一小時超過 100 次卻沒收到通知 → Sentry 頻率規則在新 workflow engine 下沒有評估，用 UI 重建。
- 第一次遇到讀不懂的前端 stack → 補 source map 上傳（要 CI secret `SENTRY_AUTH_TOKEN`）。
- 開始保管外部使用者的資金，或發生 Telegram 訊息被讀或被刪的事件 → 重評 B2（CI 用獨立 bot）。
- Sentry 的條款、價格或資料位置不再適合 → 換 DSN 到自架（不在同一台 VM）。

## Related

- 壓縮自 gitignored 的品質基線 Prompt 3 計畫（2026-10-12 起）的 D2、D5、D8、D17 Q3；該目錄從未進版控，內容僅存本 ADR。
- [2026-07-27-halt-control-and-behaviour-observability](2026-07-27-halt-control-and-behaviour-observability.md)：halt 的狀態與 Followup。
- `docs/runbooks/operations.md`（告警 runbook）、`docs/runbooks/secret-rotation.md`（Telegram token）。
