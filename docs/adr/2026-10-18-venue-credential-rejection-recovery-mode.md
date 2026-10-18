---
title: venue 拒絕憑證成為明確狀態：開機時進憑證恢復模式、執行中 exit 78，換憑證比對存下的 Bitfinex user id
date: 2026-10-18
status: active
amends:
  - "[2026-10-15-tailscale-native-operator-ingress-and-cli-only-high-risk-operations](2026-10-15-tailscale-native-operator-ingress-and-cli-only-high-risk-operations.md) Amendment D4b（換憑證由 bot 驗證與啟用）：worker 改在憑證被拒時仍執行"
tags: [bfx-funding-bot, decision, security, credentials, boot, architecture]
---

# venue 拒絕憑證：憑證恢復模式與存下的 user id

## Context

[2026-10-15](2026-10-15-tailscale-native-operator-ingress-and-cli-only-high-risk-operations.md) Amendment D4b 讓換憑證只走 outbox：CLI 把新 key 封裝給執行中的 bot（`credential_rotation_recipients`），由 bot 的 `CredentialRotationWorker` 驗證並啟用（[2026-08-31](2026-08-31-account-isolated-execution-target-architecture.md) D5'）。接著要刪掉 webapi 的憑證端點，但讀碼發現兩個缺口：

- **開機時 key 被拒是死結。** worker 在開機觀測成功之後才啟動；Bitfinex 對撤銷或錯誤的 key 回 HTTP 500＋`["error",10100,"apikey: …"]`（生產環境觀察過），開機觀測把它判為非暫時性的拒絕，process 以 exit 1（`boot_refused`）結束，container 重啟後再被拒。worker 永遠沒有執行，CLI 拿不到收件公鑰（`bot_not_running`）。文件原本寫「憑證失效 → exit 78」，與程式不符：exit 78 只在撤單遇到字面上的 401／403 時發生。
- **撤銷的 key 無法證明帳號。** worker 比對新 key 與現行 key 是否屬於同一個 Bitfinex 帳號，方法是用現行 key 即時查 `v2/auth/r/info/user`；現行 key 已撤銷時查不到，輪替回 `account_identity_unavailable`。

**約束**：

- `external` Bitfinex 的新 key 只能由人在網站產生；撤銷的 key 無法再查 user id（推論：撤銷 key 打 `info/user` 的回應同為 10100，未實測）。所以復原必有一個人工步驟，比對要靠事先存下的 id。
- `external` KEK 在 OCI Vault 之前都放在 bot 的 env；把它交給另一個 one-shot 程序等於多一個持有者。
- `inherited` 「CLI 不持有 KEK、憑證由 bot 驗證與啟用」（2026-10-15 D4b）：我方決定，可修訂。
- `inherited` worker 在開機觀測之後才啟動（`Daemon.run`）：我方程式，正是死結的來源。
- `inherited` `BOT_SETTLE_WINDOW`（開機 10 分鐘內不重啟）與部署健康閘門：bfx-deploy 在 bot 開機後做 health wait 與 settle，期間重啟會判部署失敗，套過 migration 時 bot 會被停掉。新設計必須避開，否則組成新的死結。

## Options Considered

- **基準**：資料面的憑證失效時，資料面停止並對外標示，控制面照跑，修好後自動恢復，不靠重啟循環。Kubernetes controller 在雲端憑證失效時於 status 標 `Degraded` 並在行程內重試，新 secret 經 API 送進來後下一輪 reconcile 就用上，不需要 controller 先開機成功；Envoy SDS、Vault Agent 的 secret 輪替走獨立通道熱更新，接收新 secret 不依賴舊 secret 有效。交易系統的對應做法（原則，未對照具體產品）是 venue 拒絕憑證即停止送單、不撤單不改帳，保留換 key 的管理通道。
- **A. 「憑證被拒」成為明確狀態，bot 留在恢復模式**：新增分類（10100 或 HTTP 401／403）；開機時被拒不結束，只跑換憑證 worker 與 liveness，行程內慢速重試開機觀測；存 Bitfinex user id 供撤銷後比對。
- **B. 擴充 bootstrap 腳本成「取代失效憑證」的 one-shot**：bot 映像加 bot env，持 KEK，經 `bfx-op` 執行。不碰 daemon，程式最少。
- **C. 只存 user id，開機死結先靠 webapi 憑證端點兜底，延到 9g（KEK 搬進 OCI Vault 與定期權限重查）才刪端點**。

## Decision

- **D1 分類**：`VenueCredentialRejectedError`（`external/bitfinex/errors.py`，`BitfinexAPIError` 的子類）＝回應 body 的 venue code 為 `ERR_AUTH_FAIL`（10100），或 HTTP 401／403。所有簽名讀取的 choke point `_signed_post` 依此選型別；`credential_rejection(exc)` 沿 `__cause__`／`__context__` 找它。nonce 10114、`BootInvariantError`、schema／realm 錯誤不屬於這類，照舊拒絕開機。送單結果的分類（UNKNOWN）與 `ExecutorAuthError` 的處理不變。
- **D2 開機時被拒 → 憑證恢復模式**：`_boot_until_observed` 回 `CREDENTIAL_REJECTED`，`Daemon.run` 改跑恢復模式：只啟動 `CredentialRotationWorker`、`health_check`、`db_keepalive`、healthz 與 writer lock liveness，不跑任何交易 task，也不跑 trading control。`/healthz` 200（部署進恢復模式算部署成功）、`/readyz` 503，發一則 CRITICAL `credential_recovery_mode` 告警。開機觀測在行程內以 1、2、4 分鐘、之後每 5 分鐘重試一次。
- **D3 恢復模式的出口一律是乾淨重啟**：輪替 applied（worker 照舊重啟）、重試的開機觀測成功、或 DB 的 active 憑證已不是開機載入的那把 key（webapi 端點換的），都設 stop event、`clean_stop`、exit 0，由 restart policy 開一個新 process 正常開機。重啟一律等到這次 run 滿 `BOT_SETTLE_WINDOW`。
- **D4 執行中被拒 → exit 78**：periodic reconcile 遇到 D1 的拒絕時往外丟（不再算一次抓取失敗），`_run_daemon` 在 `except* Exception` 之前以 exit 78 結束；重啟後由開機進恢復模式。偵測延遲最多一個 reconcile 週期。
- **D5 存 Bitfinex user id**：`exchange_account_credentials.bitfinex_user_id`（nullable，正值 CHECK）。輪替 applied 與 bootstrap 寫入；active 列為 NULL 時，bot 在正常模式下用開機載入的 key 查一次補上（trust on first use，以 key 定位列，失敗後最多每小時再試，恢復模式不試）。worker 的帳號比對用被取代那一列存下的 id；為 NULL 時退回用現行 key 即時查詢。
- 選 A，不選 B、C。

## Rationale

- **選 A 不選 B**：B 是 outbox 之外第二條寫憑證的路徑，沒有 `operator_authorized` 檢查、沒有 outbox 紀錄，牴觸 2026-10-15 D4b，還要讓一個 one-shot 程序持有 KEK；而且它不處理執行中被拒。A 讓唯一的寫入路徑（outbox）在憑證失效時仍可用，正是基準說的「控制面不依賴資料面」。
- **選 A 不選 C**：C 讓 webapi 繼續持有 KEK、打 Bitfinex、寫憑證表，`bfx_webapi` 對憑證表的 INSERT／UPDATE 收不回。C 不是在等任何外部依賴（A 不需要 OCI Vault），只是把同一件事延後。
- **重啟而不在行程內切換（D3、D4）**：執行中被拒若在行程內取消交易 task，已經 beat 過的 liveness key 會變 stale，`/healthz` 轉 503、staleness 掃描在 3 倍門檻時丟 `FatalError`；要在行程內切換就得清 probe 狀態。重啟免費得到乾淨的狀態，代價是一次重啟。開機恢復成功也重啟，是因為恢復模式沒有建立任何交易 task，正常開機的路徑只有一條。
- **恢復模式守 `BOT_SETTLE_WINDOW`**：部署進恢復模式時 `/healthz` 200，bfx-deploy 照樣 health wait 與 settle；這段期間重啟會判部署失敗。恢復模式本身不重啟、run 開始時間固定，operator 最多等一次 10 分鐘。
- **不跑 trading control**：恢復模式下 kill 的 cancel-all 會用被拒的 key，被記成 `failed`，catch-up 把 `failed` 視為終態，恢復後 HALTED 但掛單留在 Bitfinex。不跑時 kill 留在佇列，正常開機後以可用的 key 套用並撤單，與原本 exit 1 重啟期間的行為相同。恢復模式本來就不送單，不需要 halt 才安全。
- **重試的就是開機本身**：不另做探測，所以不會出現「探測成功、開機仍失敗」的重啟循環。
- **代價**：改動開機與執行中的停單條件（交易路徑），因此有回滾段；多一個 nullable 欄位與一個恢復模式的 TaskGroup；webapi 端點在 9c-2b 刪除前，它建立的新 active 列要等下一次正常開機才補上 user id。

## Expected Outcome

- 開機時 key 被拒：bot 不再 crash loop，CLI 可以送輪替，applied 後自動重啟回正常交易；key 在 Bitfinex 端恢復時也自動回來。
- 執行中 key 被拒：最多一個 reconcile 週期後 exit 78，重啟進恢復模式。
- 舊 key 已撤銷時仍可輪替（比對存下的 id）；新 key 屬於另一個帳號時拒絕。

## Followup

- 刪除 webapi 憑證端點前，在 VM 唯讀確認所有 Bitfinex active 憑證的 `bitfinex_user_id` 非 NULL；刪除時以 migration 加 CHECK（active ⇒ 非 NULL），之後只有輪替與 bootstrap 會建立 active 列，兩者都寫 id。
- 終局是單一狀態「venue 憑證不可用＝停止送單、控制面照跑、修好後自動恢復」。觸發：實作開機與每 6 小時的權限重查與 IP 白名單時，「不合規」併入同一個進入點與告警（換 reason），不另開第二種停單狀態。

## Revocation Triggers

- Bitfinex 改變拒絕憑證的回應形狀（不再是 10100 或 401／403）→ 重訂 D1 的分類。
- 換憑證不再經由 bot（例如改由外部 secret manager 熱更新）→ 重評 D2、D3。

## Rollback

- 刪除 webapi 憑證端點之前：roll forward 拿掉恢復模式接線與 reconcile 的 re-raise，回到 exit 1 重啟；webapi 端點仍可兜底。欄位留著無害（forward-only）；存下的 id 比對可以保留（NULL 時退回即時查詢）。
- 刪除端點之後：恢復模式不可拿掉；要拿掉先恢復 webapi 憑證端點。

## Related

- 本 ADR 即原始紀錄：2026-10-18 Will 在 coding agent 對話中選定 A（含不做 VM 演練、重試 1→5 分鐘照預設），並同意「恢復與執行中被拒都經一次重啟、恢復模式不跑 trading control」的修訂。
- [2026-10-15-tailscale-native-operator-ingress-and-cli-only-high-risk-operations](2026-10-15-tailscale-native-operator-ingress-and-cli-only-high-risk-operations.md) Amendment D4b、[2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md) D5'、[2026-10-07-loop-watchdog-and-dependency-readiness-replace-autoheal](2026-10-07-loop-watchdog-and-dependency-readiness-replace-autoheal.md)（liveness 與 readiness 的分工）。
- 程式：`external/bitfinex/errors.py`、`modules/marketfeed/daemon.py`（`_run_credential_recovery`）、`modules/execution/periodic_reconcile.py`、`modules/execution/credential_rotation.py`、migration `1d34a3306e02`；runbook `docs/runbooks/operations.md` 的 `credential_recovery_mode` 與 §11。
