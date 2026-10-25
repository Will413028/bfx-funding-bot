# 資安事故與外洩

懷疑秘密、交易所憑證、operator 裝置或主機被他人取得時照這份做。目標順序：先停止曝險，再保存證據，
再換掉受影響的東西，最後寫紀錄。不確定是不是事故時，當成事故處理。

## 1. 偵測來源

- Telegram：`foreign_exposure`、`foreign_lending`（有不是 bot 下的 offer 或借出）、`nav_drop`、
  `credential_recheck_failing`、`credential_recovery_mode`（`operations.md`「6. 告警」）。
- Sentry 的新 issue、`bfx.security` 安全事件 log（Loki 保留 30 天）。
- Bitfinex 的登入紀錄、API key 清單與通知信。
- Tailscale admin console 的裝置清單與 SSH 紀錄；服務商（GitHub、Cloudflare、Sentry、Telegram）的外洩通知。
- 秘密出現在 log、chat、issue、截圖或 AI agent 輸出（`secret-rotation.md`「何時換」）。

## 2. 停止曝險

1. 停止交易並撤單：`sudo bfx-op kill --reason "security incident"`（`operations.md`「3. 帳戶停止與恢復」）。
   CLI 不能用時用 UI 的 Kill switch；兩者都不能用時，到 Bitfinex 網頁手動撤單。
2. 懷疑 Bitfinex key 外洩：直接在 Bitfinex 刪除那把 key。key 沒有 withdraw 權限，但仍能掛單與撤單。
   刪除後 bot 進入憑證恢復模式，不再送單（`operations.md`「11. 換交易所憑證」最後一段）。
3. 懷疑 operator 裝置或 tailnet 身分外洩：在 Tailscale admin console 移除該裝置，必要時暫停營運入口：
   `sudo docker stop bfx-tailscale`（UI 停用，bot 不受影響）。
4. 懷疑主機被入侵：保持 kill，先做第 3 節，再依 `offsite-dr.md` 在新主機重建，新主機的秘密一律換新值。
5. 懷疑 GitHub 帳號或 CI 被盜：先停自動部署，`sudo systemctl stop bfx-deploy.timer`（[deploy §7](deploy.md#7-暫停自動部署)）。
   `bfx-deploy` 每 5 分鐘部署最新的綠燈 `main`，provenance 驗證只證明 image 由本 repo 的 release workflow 產生，
   擋不住拿到 repo 寫入權的人自己 merge、自己觸發 release。接著：
   - 在 GitHub 撤銷 session 與不認得的 PAT、SSH key、deploy key、OAuth app，重設密碼與第二因子。
   - 對照 `main` 最近的 merge 與部署紀錄（[deploy §3](deploy.md#3-常用指令vmroot)「部署紀錄」）：有沒有不認得的 commit 被部署過。
   - 有：那個 image 帶著 bot 的秘密跑過，照第 4 點當成主機被入侵，並照第 4 節換掉所有秘密。
   - 沒有：revert 可疑的 commit（PR → CI），確認 `bfx-deploy --dry-run` 要部署的是預期的 revision 後再
     `sudo systemctl start bfx-deploy.timer`。repository secrets（Telegram）照 `secret-rotation.md` 換掉。
6. 懷疑公開站被竄改（`https://borrowrate.pages.dev` 的內容與 VM 產生的不同，或每日 `public-site` 檢查失敗而 VM 沒有上傳）：
   - 先在 Cloudflare dashboard 撤銷 Pages token（它是帳戶層級，能改同帳戶的所有 Pages 專案）。
   - 看 Pages 專案的 deployment 清單：不是 VM 上傳時間的 deployment 就是外部上傳；同帳戶的其他 Pages 專案也一起看。
   - 照 `secret-rotation.md`「Cloudflare Pages token」發新 token，`sudo systemctl start bfx-public-site.service` 從 VM
     重發一版；journal 最後一行是 `published 6 files` 才算還原。
   - 公開站碰不到 VM 與 DB，交易不受影響，不需要 kill。

## 3. 保存證據

在換東西之前先保存，換完之後有些證據就不存在了。

- 時間範圍內的 container log：`sudo docker logs --since <UTC> bfx-bot`、`bfx-webapi`；Loki 只留 30 天，要匯出。
- 主機的 journald：`sudo journalctl --since <UTC> -u 'bfx-*'`、Tailscale SSH 紀錄。
- DB：`deployments`、`trading_control_requests`、`funding_cancel_all_audit` 與 journal 的相關列（以 `bfx_operator`
  或 owner 唯讀查詢）；需要完整快照時跑一次 pgBackRest 備份。
- Bitfinex 的 API 使用與登入紀錄截圖，與 bot 的 journal 比對：bot 沒有下過的 offer 或撤單就是外部操作。

證據存在 repo 以外的地方（含秘密值或個資的不放進 issue 或 PR）。

## 4. 換掉受影響的東西

照 `secret-rotation.md`，只換受影響的；不確定範圍時整張表從上到下換一次。Bitfinex key 走
`sudo bfx-op rotate-credential`（`operations.md`「11. 換交易所憑證」）。全部換完、`sudo bfx-op status` 正常後，才以
`sudo bfx-op resume` 恢復交易。

## 5. 紀錄與通報

- 依 `docs/incidents/TEMPLATE.md` 寫事故紀錄；修補 PR 帶先紅後綠的回歸測試（根目錄 `AGENTS.md`）。
- repo 是 public：修好之前，紀錄與 PR 不寫可利用的細節（`SECURITY.md`）。
- 個資：目前只有 operator 本人的資料，不需要對外通報；開放其他使用者之後依個資法第 12 條通知當事人
  （`backend/ARCHITECTURE.md` §10f）。
