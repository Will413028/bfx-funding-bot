# 上線清單：某個幣別開始用真錢放貸之前

每次「某個幣別第一次啟用」或「停了一段時間後恢復真錢放貸」之前逐項核對。每一項都要有當下量到的證據
（指令輸出、ledger 列、告警截圖），不能沿用舊的。任何一項不過就不啟用；啟用本身是最後一步。

| # | 項目 | 怎麼確認 | 出處 |
|---|---|---|---|
| 1 | 正在跑的 release 是綠燈的 `main` | `deployments` 最新一列是 `deployed`，`source_revision` 等於 `main` 上 CI 綠燈的 commit | `deploy.md`「1. 流程總覽」 |
| 2 | DR 就緒 | 一份新的備份與隔離還原證據符合每道關卡（offsite DR §8），timer 都已啟用（§9）；只有這樣才算 DR ready（§10） | `offsite-dr.md` §8–§10 |
| 3 | 最近一次 restore test 通過 | 每月的 ledger restore test 在 31 天內通過 | `offsite-dr.md`「Monthly and change-triggered ledger restore test」 |
| 4 | 回滾路徑可用 | rollback drill 在最近一次帶 migration 的 release 之後跑過；知道帶 migration 的 release 只能 roll forward | `deploy.md`「5. 失敗與回滾」「6. Rollback drill」 |
| 5 | 告警端到端送得到 | 兩條路徑都在 Telegram 收到：bot 的 `send_test_alert`、主機的 `bfx-notify --level info "test"`；Grafana 規則與 healthchecks.io heartbeat 都是 OK | `operations.md`「6. 告警」 |
| 6 | SLO 有現值 | `docs/slo.md` 的三個查詢都在近 7 天內跑過，沒有低於目標的 | `docs/slo.md` |
| 7 | 憑證合規 | bot 的開機與每 6 小時檢查通過：API key 沒有 withdraw 權限 | `backend/ARCHITECTURE.md` §10c |
| 8 | 包絡設好 | 該幣別的 CapitalPolicy 包絡（利率下限、天期、單筆上限）以 dry run 確認過，金額與預期相符 | `operations.md`「2. 幣別啟停與包絡設定」 |
| 9 | 交易狀態乾淨 | 沒有 operator kill、沒有 `HALTED`（第 3 級）、沒有隔離中的幣別或未結案的 UNKNOWN（第 2 級） | `operations.md`「3. 帳戶停止與恢復」「4. 異常的四個等級」「5. UNKNOWN 的人工處理」 |
| 10 | 事故流程就位 | 讀過 `docs/runbooks/security-incident.md`；事故紀錄範本在 `docs/incidents/TEMPLATE.md` | 根目錄 `AGENTS.md`「測試與品質」 |

全部通過後啟用（只走 CLI）：

```bash
sudo bfx-op enable <symbol> --reason "上線清單 YYYY-MM-DD 全數通過"
```

啟用後一個 reconcile 週期內確認第一個 accepted observation 與第一個 ack 的 submit；沒出現就先查，不要放著等。
