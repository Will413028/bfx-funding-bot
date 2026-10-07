---
title: migration 在自己的交易內斷言資料前提與結果，部署先在隔離還原副本上演練新 image 的 migration；不再靠人工查 prod
date: 2026-10-08
review-date: 2027-01-08
status: active
tags: [bfx-funding-bot, decision, database, migration, deploy, dr]
---

# migration 自帶斷言，部署先在隔離副本演練

## Context

prior state：含 migration 的部署由 bfx-deploy 依序停 bot → 備份 → 隔離 restore test → prod `alembic upgrade head` → 起新版，任一步失敗 bot 維持停止、只能 roll forward（[2026-09-25-automated-probation-replaces-release-ceremony](2026-09-25-automated-probation-replaces-release-ceremony.md) D6）。restore test（`deploy/vm/pgbackrest/restore_drill.py --restore-test`，[2026-10-06-ledger-restore-verification-replaces-prefix-test](2026-10-06-ledger-restore-verification-replaces-prefix-test.md)）還原最新備份、比對 ledger、用**現行部署的** image 跑唯讀 boot check；它不跑新 release 的 migration，結束即清除副本。所以新 migration 第一次碰到真實資料就是在 prod 上。

觸發：operator request outcome 重構（[2026-10-08-operator-requests-insert-only-with-insert-once-outcome](2026-10-08-operator-requests-insert-only-with-insert-once-outcome.md)）要 backfill 歷史列，計畫原本寫「先 spike 查 prod 資料形狀（隔離副本上查），每個 release 部署後由 owner 在 `BEGIN READ ONLY` 內跑核對 SQL」。動工前發現 restore drill 沒有跑自訂 SQL 或保留副本的選項，前者照字面做不到；而兩者都是一次性人工步驟，每支會動資料的 migration 都得再來一次。

**約束**：

- `external` 正式環境在線、真錢放貸中；沒有外部使用者，允許計畫性 halt（[2026-09-28 ADR](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md) Context）。
- `inherited` bot 在 schema head 不符時拒絕開機（`core/schema_head.py`），回滾只能 roll forward；新 migration 為 forward-only（[2026-10-08-forward-only-migrations](2026-10-08-forward-only-migrations.md)）：仍成立，前者是 live daemon 的安全檢查，後者是 venue 寫入不可撤回的直接結果。
- `inherited` bfx-deploy 用目標 release 的 DR 腳本，但主機工具（bfx-deploy、wrapper、unit）在部署成功後才安裝、下一輪才生效；改 drill／boot JSON／wrapper 參數要證明新舊方向都過（2026-10-06 ADR 約束）：仍成立，是部署工具的自舉方式。
- `inherited` 每次 ssh 到 VM 都要 Will 授權：仍成立，是操作授權規則。
- `inherited` 失敗一律維持停 bot（09-25 D6）：當時的理由是 migration 可能已部分套用；演練失敗時 prod schema 沒動，這個理由不成立，見 D3。

## Options Considered

- **基準. migration 在 production clone 上自動實跑，並自帶資料前提斷言**：GitLab 對新增 migration 的 MR 在 Postgres.ai thin clone 上實跑（「automatically test migrations in a production-like environment」），狀態不是 `success` 不能 merge（[GitLab: Database migration pipeline](https://docs.gitlab.com/development/database/database_migration_pipeline/)）；Atlas 讓每個 migration 版本帶「a list of assertions (predicates) that must evaluate to true before the migration is applied」，失敗就不套用（[Atlas: Pre-Execution Checks](https://atlasgo.io/versioned/checks)）。
- **A. 維持人工查詢**：spike 改由 owner 在 prod 的 `BEGIN READ ONLY` 內跑，部署後核對照舊。零改動；結論不編碼、到部署當下可能已過時，且每次都要授權 ssh。
- **B. 只做 migration 內斷言**：前置斷言（違規列筆數＝0）與後置斷言（backfill 結果逐欄相等、撤權確實生效）寫在 migration 的同一個交易內，不符就 RAISE、整支回滾（`backend/alembic/env.py` 單一交易；本 repo 先例 `f5a6b7c8d9e0`）。不改部署工具；斷言第一次觸發仍在 prod，失敗時 bot 依 09-25 D6 維持停止到下一版。
- **C. B＋部署前演練（基準在單機上的形態）**：restore test 在既有驗證之後，用候選 image 對隔離副本跑 `alembic upgrade head` 與候選 image 的 boot check；失敗就擋下部署、prod 不動。
- **D. 在 PR 階段演練（GitLab 原樣）**：CI 為 PR 建 image、VM 先演練再允許 merge。回饋最早；但 CI 只對 main 建 image、`bfx-deploy` timer 自動部署 main，要新增 PR image 管線與觸發。
- **E. 常駐唯讀查詢 role**（`pg_read_all_data`＋`default_transaction_read_only`）：ad-hoc 查詢的標準做法，與上面互補而非替代。

本決策的選項由一個未參與先前討論的 subagent 從零起草（附業界來源與約束分類），主 session 抽查其引用的程式位置與外部頁面後交 Will。

## Decision

- **D1**：採 C。每支會改動或搬移資料的 migration 在自己的交易內寫前置與後置斷言，RAISE 訊息帶違規筆數；不再有部署前 spike 與部署後人工核對 SQL。
- **D2**：restore test 增加演練：在現行 image 的 boot check 之後（現行 image 在新 head 上會拒絕開機），用 label `revision` 等於 drill 自身 checkout 的候選 image 跑 `alembic upgrade head` 與 boot check。monthly 執行時候選＝現行，演練為空操作。演練以獨立的 release（R0）先上線，因為主機工具晚一輪生效。
- **D3**：修訂 09-25 D6：演練失敗時 prod schema 未動，bfx-deploy 重啟舊 bot（與無 migration 的部署失敗同類）；備份、還原或 prod migration 本身失敗仍維持停止。
- **D4**：owner `BEGIN READ ONLY` 的 prod 查詢只作 break-glass，不寫進任何 release 的完成定義。

## Rationale

- **C 而非 A**：A 的事實停在查詢那一刻，B／C 的斷言在 migration 那一刻、同一個交易內檢查，不會過時；人工步驟每支 migration 都要重做，斷言寫一次就跟著 migration 走。
- **C 而非只做 B**：B 單獨用時，斷言失敗的代價是一次 prod 停機（bot 停到下一版）；C 讓同一組斷言先在副本上觸發，副本取自停 bot 之後的備份，資料與 prod 幾乎相同。代價是改 drill 與 bfx-deploy、多一個 R0 release，以及 restore test 的時間多一次 migration。
- **不選 D**：沒有外部使用者、允許計畫性 halt，演練失敗只是修好再出一版，merge 前回饋的價值抵不過新增 PR image 管線的成本。
- **E 延後**：目前沒有反覆出現的 ad-hoc 查詢需求；上一個專用讀取 role（`bfx_cutover_reader`）用途結束後已刪。
- **D3**：09-25 D6「失敗維持停機」是為了 migration 可能已部分套用的情況；演練失敗時 prod 沒有任何變更，維持停機只是把可用時間換成等待，沒有換到安全性。

## Expected Outcome

- 會動資料的 migration 第一次碰到真實資料是在隔離副本上；前置斷言失敗時 deploy ledger 記 restore test 失敗、bot 仍在跑舊版。
- release 的完成定義不含人工 prod SQL；restore 收據帶演練結果。

## Followup

- R0：實作 D2、D3（含 runbook `docs/runbooks/deploy.md`、`docs/runbooks/offsite-dr.md`），驗收含新舊方向與「演練失敗 → bot 重啟、備份失敗 → bot 停止」的測試。副本上以哪個 role 跑 migration（需 table owner）在實作時決定並寫進 PR。
- operator request outcome 的 R1 依 D1 寫斷言，是第一個被演練的 migration。

## Revocation Triggers

- 出現外部使用者，或不再允許計畫性 halt → 重評 D（PR 階段演練）。
- 同類 ad-hoc prod 查詢需求第二次出現 → 建 E。
- restore test 加上演練後超過 DR 的時間預算 → 演練拆成獨立步驟。

## Review Notes

## Related

- 來源：2026-10-08 Will 在 coding agent 對話中選定 C（含 R0 先出），本 ADR 即原始紀錄，無其他外部來源文件。
- 修訂 [2026-09-25-automated-probation-replaces-release-ceremony](2026-09-25-automated-probation-replaces-release-ceremony.md) D6（已加 Amendment 指回本檔）；延伸 [2026-10-06-ledger-restore-verification-replaces-prefix-test](2026-10-06-ledger-restore-verification-replaces-prefix-test.md) 的 restore test。
- `docs/runbooks/deploy.md` §1 步驟 6、`docs/runbooks/offsite-dr.md`「Monthly and change-triggered ledger restore test」。
