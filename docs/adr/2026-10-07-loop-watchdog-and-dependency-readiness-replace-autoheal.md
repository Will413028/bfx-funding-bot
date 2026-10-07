---
title: event loop 卡死由 process 內 watchdog 自行退出、外部依賴改走 readiness 而非重啟，取代 autoheal
date: 2026-10-07
status: active
supersedes: "[2026-05-31-koyeb-to-vm-single-writer-lock](2026-05-31-koyeb-to-vm-single-writer-lock.md) 的 D3（unhealthy 重啟）"
tags: [bfx-funding-bot, decision, deployment, safety, observability]
---

# event loop 卡死由 process 內 watchdog 退出，外部依賴走 readiness

## Context

[2026-05-31-koyeb-to-vm-single-writer-lock](2026-05-31-koyeb-to-vm-single-writer-lock.md) D3 用 compose healthcheck＋`autoheal` sidecar 重啟 unhealthy 的 bot。2026-09-25 改成 CI digest 部署（[2026-09-25-ci-registry-digest-deploy](2026-09-25-ci-registry-digest-deploy.md)）時，新寫的 `docker-compose.app.yml` 沒有帶入 healthcheck 與 autoheal，也沒有記錄。2026-10-07 在 prod 確認：`bfx-bot` 的 Healthcheck 為 null、RestartPolicy 為 `unless-stopped`。

結果有兩個缺口：

- process 活著但 event loop 卡死時，`/healthz`（uvicorn，同一個 loop）與 `scan_staleness`（同一個 loop）都不會執行，沒有任何東西重啟它，只剩 Grafana「5 分鐘無 log」與 healthchecks.io 的告警，要人手動重啟，違反「放貸全自動」。
- liveness 表混入外部依賴：`ws` 只在 Bitfinex 送 frame 時記、`db_keepalive` 只在 `SELECT 1` 成功時記。Bitfinex 斷線約 270 秒或 DB 斷線約 21 分鐘就會 FatalError 重啟，重啟後開機觀測連不到 venue 又失敗，形成 crash-loop。

**約束**：

- `external` 單機 Docker Engine（非 Swarm）不會重啟 `unhealthy` 的 container（[moby#28400](https://github.com/moby/moby/issues/28400)），只對 process 退出套用 restart policy。
- `external` bot 是 Docker container，不是 systemd service，沒有 `NOTIFY_SOCKET`；本步驟不換平台。
- `external` asyncio 是單一 thread；卡住的程式可能佔住 GIL，process 內的 Python thread 不保證能執行。
- `external` prod 沒有 event loop 延遲的分布數據。
- `inherited` container 不持有 docker.sock、app 服務禁止 volume／tmpfs、image 只以 digest 部署（`deploy/vm/ops/compose_policy.py`、[2026-09-25-ci-registry-digest-deploy](2026-09-25-ci-registry-digest-deploy.md)）：仍成立，理由是 container 被入侵時不能取得 host 控制權。
- `inherited` 單一寫入者 session advisory lock，失去 lock 立即停止（2026-05-31 D1）：仍成立，Bitfinex 沒有 fencing token。

## Options Considered

**恢復機制**

- **基準：工作迴圈送心跳、迴圈外的計時器逾時就殺掉 process、由 supervisor 重啟**：[systemd `WatchdogSec`](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html#WatchdogSec=)、[Erlang `heart`](https://www.erlang.org/doc/apps/kernel/heart.html)；liveness 只看 process 本身、外部依賴放 readiness（[Kubernetes probes](https://kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes/)）。
- **A. 維持現狀**：只告警。
- **B. 加回 healthcheck＋autoheal sidecar**（05-31 D3）。
- **C. healthcheck＋host 上的 systemd timer 重啟 unhealthy container**。
- **D. process 內 watchdog**：Python 標準庫 [`faulthandler.dump_traceback_later(T, exit=True)`](https://docs.python.org/3/library/faulthandler.html#faulthandler.dump_traceback_later) 是不需要 GIL 的 C 層計時器，loop 上的 task 每 T/4 重新上膛；外部依賴移出 liveness。

**watchdog 退出後的告警**

- **基準：由監督者／觀察者回報重啟**（Kubernetes restart 計數→Alertmanager、systemd `OnFailure=`），不靠將死的 process 自己發。
- **A. 另一個 thread 先送 Telegram 再退出**。
- **B. 下次開機偵測上一次 run 沒有結束紀錄**。
- **C. VM 上的 Loki 規則比對 faulthandler 輸出**。

## Decision

- **D1 恢復選 D**：`BFX_LOOP_WATCHDOG_S` 預設 120；從 bot run 一開始上膛（涵蓋 build 與開機觀測），graceful shutdown／drain 前解除。task 層的卡死仍由 `scan_staleness` → FatalError 處理。
- **D2 liveness 只看自身迴圈有沒有在跑**：外部依賴（`ws_data`、`db`）另列一類，只影響 `/readyz` 與送單 guard，不觸發 FatalError、不讓 `/healthz` 回 503。開機後從未見過的依賴視為過期；依賴過期期間送單 fail-closed；撤單不因市場資料新鮮度被擋。
- **D3 開機遇依賴不可達時在 process 內等待**：有上限的 backoff 重試、不送單；例外鏈上任何一環是明確拒絕（FatalError、TLS 憑證驗證、權限錯誤、venue 的 `["error", code, …]`，rate limit 與 maintenance 除外）就拒絕開機。
- **D4 告警選 B**：新表 `bot_runs` 記錄每次 run 的開始與結束原因；開機時把同一 scope 沒有結束紀錄的 run 標為 `unclean`，並對每筆發一次 CRITICAL 告警。
- **D5 不加 healthcheck、autoheal、docker.sock**。`/healthz` 與 VM 上的告警保留為後備。

## Rationale

- **D1 選 D 而非 B**：B 要把 docker.sock 交給 container、用未釘 digest 的 image，違反 container 隔離；而且 healthcheck 的 90 秒門檻比現有 3 倍門檻更早因外部依賴中斷而重啟。**不選 C**：要與部署共用 lock、只動 running＋unhealthy、限制重啟次數，多一個 host 元件卻只解決 D 已涵蓋的情況。**代價**：整個 process 被凍結（SIGSTOP、D-state）時 D 的計時器也不跑，改由外部告警發現；合法但超過 T 的同步呼叫會被殺，所以 T 先保守取 120 秒。
- **D2**：重啟救不了外部依賴中斷，只會把它變成 crash-loop；正確行為是停止交易、等依賴恢復。撤單不需要行情，擋撤單會讓 HALTED 時的自動撤單撤不掉。
- **D3**：重試要能區分「連不到」與「對方明確拒絕」：Bitfinex 把 API key 錯誤包在 HTTP 500 裡，TLS 憑證錯誤被 httpx 藏在 suppressed context 裡，只看 status code 或 `__cause__` 會把它們當成暫時性而永遠等下去。
- **D4 選 B 而非 A**：卡住的程式佔住 GIL 時 A 的 thread 跑不起來；B 涵蓋 watchdog、OOM、SIGKILL 所有非正常退出，全部在 repo 內、可測試。**不選 C**：設定只在 VM、沒有版本控制。**代價**：告警本身看不到卡在哪一段，traceback 要到 container log（Loki）查；DB 斷線導致結束紀錄寫不進去時會多報一次。

## Expected Outcome

- event loop 卡死超過 120 秒，bot 自行退出並由 Docker 重啟，下次開機發一次 `previous_run_unclean` 告警；不需要人手動重啟。
- Bitfinex 或 DB 中斷期間 bot 不重啟、不送新單，撤單照常；恢復後自動繼續。
- API key 失效或 TLS 設定錯誤時開機拒絕（BOOT_REFUSED），不無限等待。

## Revocation Triggers

- 外部告警響了但 container log 沒有 watchdog traceback（整個 process 被凍結）：加上 C 的 host timer，與部署共用 lock 並限制重啟次數。
- 平台改成 systemd service 或 Kubernetes：改用原生 `WatchdogSec` 或 livenessProbe。
- 有 event loop 延遲（`bfx_event_loop_lag_seconds`）的分布數據後：把 T 收緊到 p99.9 的數倍。

## Related

- 取代 [2026-05-31-koyeb-to-vm-single-writer-lock](2026-05-31-koyeb-to-vm-single-writer-lock.md) 的 D3；D1 的 single-writer lock 不變。
- 來源：2026-10-07 Will 與 coding agent 的討論；選項由獨立 subagent 起草（業界基準、選項、約束分類）後 Will 採用 D 與告警 B。
