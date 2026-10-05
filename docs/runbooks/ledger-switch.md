# Ledger switch（legacy → ledger，一次執行）

把 production 的 capital authority 從 legacy 切到 ledger。Will 啟動一次，其餘由
`deploy/vm/ops/bfx_ledger_switch.py`（root、主機端）自動完成：停寫入者 → seed＋closure 驗證＋epoch
（同一個 transaction）→ `bfx-deploy --recreate` → 等第一個 runtime basis。沒有 unit 或 timer 會跑它
（`backend/tests/architecture/test_ledger_seed_dormant.py`）。

halt 期間**不 kill、不撤單**：resting offers 留在簿上（Will 已接受），曝險＝halt 開始時最新 legacy
snapshot 的 resting 總額，工具記進 evidence，也寫進每一則失敗通知。

## 1. 前提

- 正在跑的 release 是 dual-support build（Bitfinex 與 web API 都支援 `{legacy, ledger}`，含「ledger
  epoch 但沒有 `legacy_seed` observation 就拒絕開機」的 guard 與 legacy 表的凍結 trigger），而且它就是
  24 h soak PASS 時的 backend digest。工具只接受命令列給的這個 digest。
- soak 已結束、`bfx-sim` container 已拆掉（[simulation-soak.md](simulation-soak.md) §10）。還在跑就拒絕。
- soak 的最終判定已寫成結果檔（[simulation-soak.md](simulation-soak.md) §9「最終判定」，預設
  `/home/ubuntu/bfx/reports/sim-soak/result/soak-result.json`；別的路徑用 `--soak-result <path>`）。
- 不在週一 04:17 UTC 的 weekly report 期間（工具會檢查 service，並在切換期間停掉 timer）。

## 2. 啟動（Will，VM，root）

先只跑 P-checks（不停任何東西）：

```bash
OPS=/usr/local/lib/bfx-ops/current/ops
sudo $OPS/.venv/bin/python $OPS/bfx_ledger_switch.py preflight --digest sha256:<soak 通過的 digest>
```

通過（exit 0）就正式跑。用 transient unit 跑，SSH 斷線不會中斷 halt：

```bash
sudo systemd-run --unit bfx-ledger-switch-$(date -u +%Y%m%d) --collect --wait \
  -p TimeoutStartSec=12h -p Environment=HOME=/root \
  $OPS/.venv/bin/python $OPS/bfx_ledger_switch.py run --digest sha256:<soak 通過的 digest>
journalctl -u 'bfx-ledger-switch-*' -f   # 另一個 shell 看進度
```

之後不需要任何手動步驟；結果以 Telegram 通知（§4）與 evidence（§6）為準。

12 h 大於兩次嘗試各段上限的總和（deploy flock 30 分、snapshot 10 分、備份 2 h、seed 15 分、recreate 1 h、
上線 15 分）。工具收到 SIGTERM（逾時或 `systemctl stop`）時當成目前這一步失敗：照 §5 走對應分支、恢復 timer、
發通知。

## 3. 它做什麼

**P-checks**（任一失敗就結束，什麼都不停，legacy 照跑）：

| 檢查 | 失敗 code |
|---|---|
| deployments ledger 最新一筆是 `deployed`，而且就是最後成功的那一筆、digest 等於命令列的 digest | `last_deploy_not_successful` / `running_digest_is_not_the_soaked_digest` |
| `bfx-bot`、`bfx-webapi` 都在跑，image 是 `<repo>@<digest>` | `running_digest_mismatch:*` / `legacy_not_running:*` |
| 該 image 的 one-shot `alembic current` 等於 `alembic heads` | `schema_not_at_head` |
| soak 結果檔存在且 `verdict` 是 PASS、`image_digest` 等於命令列的 digest、最後一代的 `last_service_version` 等於該 digest 在 deployments ledger 的 revision | `soak_result_missing` / `soak_result_not_pass` / `soak_result_digest_mismatch` / `soak_result_revision_mismatch` |
| `bfx-weekly-report.service` 沒在跑 | `weekly_report_running` |
| 沒有 `bfx-sim` container | `simulation_running` |
| 整個 cluster 沒有其他 runtime session（`bfx_bot`／`bfx_webapi` 或其成員，或任何連 `bfx_sim` 的 session），bot 與 web API 自己 container 的除外 | `runtime_session_present` |
| seed `--check`（READ ONLY、不取鎖、不寫）exit 0；只有「submit／query 進行中」的拒絕（`attempt_outcome_missing`、`legacy_query_pending`）會隔 30 秒重看，最多三次 | `seed_check_refused:<reasons>` |

**步驟**（evidence 每一步都記開始／結束 ms 與結果）：

1. 停 `bfx-deploy.timer`、`bfx-weekly-report.timer`、`bfx-restore-test.timer`、`bfx-pgbackrest-backup.timer`
   （只記下原本 active 的，結束時只重啟那些），再拿 `bfx-deploy` 的 flock（最多等 30 分鐘）：沒有部署在跑，
   到第 8 步之前也不會有。
2. 記下最新 legacy snapshot 的 resting offers（每個 symbol 的 offered／foreign 與 offer 數）。
3. 停 `bfx-webapi`（不再有新的 operator request）。
4. 等一筆 query 開始時間晚於第 3 步的 legacy accepted snapshot（最多 10 分鐘；F5 的前提）。
5. 停 `bfx-bot`。
6. pgBackRest 備份（deployed release 的 `backup.sh --type diff`），記下新的 label。這是
   `restore-halt-backup` 唯一能還原的備份。
7. 以同一個 image 的 one-shot、owner 身分跑 seed `--switch`：DSN 從 `/opt/bfx/runtime/migrate.env`
   檔對檔寫成 0600 檔（沒寫 port 就補 5432），不印出，跑完即刪。同一個 transaction 內：seed 寫入 →
   capture-point closure verifier（所有設定的 cell）→ append epoch `ledger`（actor `ledger_seed:<run id>`，
   evidence 含 run id、seed 的 observation／basis id 與 digests、closure 結果、F7）→ READ ONLY 重算 digest
   （含 epoch 列）。任何拒絕、closure 違規或 digest 不符都整筆 rollback：沒有 ledger 列、沒有 epoch 列。
8. 放掉 flock，`bfx-deploy --recreate`（同一個 digest；bot 與 web API 以 ledger 開機），並確認 ledger 多了一筆
   `deployed`。
9. 等（最多 15 分鐘）：一筆非 seed 的 accepted `ledger_observation`、最新 runtime basis 沒有任何 symbol 是
   `unexplained_lending`、`trading_state` 是 ACTIVE、web API `/ready` 200。
10. 只回報、不回滾的兩項檢查：
    - **NAV 連續**（一次）：第一個 runtime basis 每個 symbol 的 total capital（available + offered + credits）
      對照第 2 步記下的最後一筆 legacy snapshot。相對差超過 0.5 % 就通知（halt 期間只有入帳的利息與手動的
      foreign offer 會合法地改變總額；成交與到期只是在 offered、credits、available 之間移動，理由寫在
      `NAV_TOLERANCE`）。
    - **24 小時活動 watch**：以 transient timer（`bfx-ledger-switch-watch-<run id>`，`systemd-run --on-active=24h`）
      在 24 小時後跑 `bfx_ledger_switch.py watch --run-id <id>`；這段期間既沒有 ack 的 submit、也沒有 reprice
      （我們的 offer 被撤）就通知。prod 平均每天約 1.4 筆 submit，所以切換本身不等成交。
11. 重啟第 1 步停掉的 timer，通知成功。

## 4. 通知的意思

| 等級 | 開頭 | 意思 | 要做什麼 |
|---|---|---|---|
| info | `done: capital authority is ledger…` | 切換完成 | 無；之後第一次週一報告照 PR-2 的排程核對 |
| warning | `NAV continuity: …` | 第一個 runtime basis 的總額和 halt 開始時差超過 0.5 % | 查 halt 期間有沒有入金／出金或手動 offer，再看 basis；切換不回滾 |
| warning | `post-switch watch: no acknowledged submit and no reprice …` | 切換後 24 小時沒有任何交易動作 | 查 `/admin/trading-status`、policy 與 cells |
| warning | `post-switch check … could not run` | NAV 檢查或 watch 排程本身失敗 | 手動跑 `watch --run-id <id>`（24 小時後）或查 basis |
| warning | `F7: live credits still recent_fill are …` | 仍是 `recent_fill` 的 live credit 超過某個 symbol 資本的 20 %；它們在 ledger 維持多 cell 直到結束 | 無（不中止）；知道 attribution 暫時較粗 |
| warning | `not started, P-check failed (<code>)` | 什麼都沒停 | 依 code 處理（§3 表），再重跑 |
| warning | `R1: failed before the seed committed …` | 沒有寫入，legacy 已重啟，timer 已恢復 | 依 code 處理，再重跑 |
| info | `R1: retrying the whole run once …` | F6／query 進行中的拒絕：legacy 收尾後自動重跑一次 | 無 |
| critical | `R1: … legacy restart FAILED` | 資料庫仍是 legacy，但 bot 或 web API 起不來 | `docker start bfx-bot bfx-webapi`，查 log |
| critical | `R2: the seed committed …` | epoch 已是 ledger，第一筆 runtime 寫入之前就失敗；bot 停著 | 見 §5 R2 |
| critical | `R3: the ledger runtime already wrote …` | ledger runtime 已寫入，切換沒走完 | 只能 forward-fix（§5 R3） |
| critical | `crashed (…)` | 工具本身異常結束 | 看 `docker ps` 與 evidence 判斷停在哪一步，依 §5 處理 |

## 5. 失敗分支

- **R1（seed 還沒 commit；資料庫的 epoch 仍是 `legacy`）**：`docker start bfx-bot bfx-webapi` 回到 legacy，
  timer 恢復。`attempt_outcome_missing`（F6）與 `legacy_query_pending` 代表停在 submit 或 query 中途：先等
  legacy 重啟後的一筆 accepted snapshot（boot recovery 收尾），再自動從 P-checks 重跑整個流程一次，最多一次。
- **R2（seed 已 commit，還沒有任何 runtime ledger 寫入）**：bot 維持停止，timer 恢復。通知寫明失敗 code、
  曝險與兩個選項：
  1. forward-fix：同一個 digest 開機失敗多半是設定問題。修好 runtime env 後 `sudo bfx-deploy --recreate`，
     或出修正版 release 讓 `bfx-deploy` 部署；
  2. 修不了時，原地還原 halt 備份（deploy.md §5 的唯一例外）：

     ```bash
     sudo $OPS/.venv/bin/python $OPS/bfx_ledger_switch.py restore-halt-backup --run-id <通知裡的 run id>
     ```

     只要出現任何 runtime ledger observation 就拒絕。步驟：停 timer、拿 deploy flock、停 bot／web API／
     frontend、再確認一次沒有 runtime 寫入、停 postgres、`pgbackrest restore --delta --set=<halt label>
     --type=immediate --target-action=promote`（postgres image 的 one-shot、沿用它的 volumes）、啟動
     postgres、確認 epoch 回到 `legacy` 且沒有 seed 列、**立即補一份 full 備份**、啟動 legacy、恢復 timer。
     還原失敗會留下停著的 postgres 並發 critical：照 [offsite-dr.md](offsite-dr.md) 處理。

  不要 append 回 `legacy`：seed 已把 pending 的 uncertainty request 標成 `superseded_by_authority_switch`，
  ledger 列刪不掉，之後重新 seed 會因 `ledger_not_empty` 被拒絕。
- **R3（runtime ledger 已寫入）**：只能 forward-fix，工具不停任何東西。曝險被低估時 protection 會自動 HALT，
  條件解除後自動恢復。

## 6. Evidence 與 exit code

`/var/lib/bfx-ledger-switch/<run id>/`（root 0700）：

- `evidence.json`：run id、digest、account、每一步（`step`、`start_ms`、`end_ms`、`result`、`detail`）、
  resting 曝險、備份 label、seed summary、F7 max share、`outcome`（`switched`、`precheck_failed:*`、
  `r1:*`、`r2:*`、`r3:*`）；
- `seed-<seed run id>.jsonl`：每次 `--check` 與 `--switch` 的完整輸出（不含 DSN）；
- `restore.json`：`restore-halt-backup` 的步驟與結果；
- `watch.json`：24 小時 watch 的 ack submit 數與 reprice 數。

| exit | 意思 |
|---|---|
| 0 | 成功（`run`／`preflight`／`restore-halt-backup`） |
| 2 | 另一個切換正在跑，或不是 root |
| 10 | P-check 失敗，什麼都沒停 |
| 11 | R1 |
| 20 | R2（或工具異常結束） |
| 30 | R3 |
| 40 / 41 | `restore-halt-backup` 拒絕／失敗 |
