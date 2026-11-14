# cell 上限只管掛單中的金額：實作計畫

**Goal：** 每份新寫的 capital basis 裡，`accepted_capital_basis_cell` 的金額只等於該 cell 掛著的受管 offer 的剩餘金額；已借出的 credit 只計入幣別的 `credits` 與總量 `T`，不計入任何 cell。`basis.py` 不再有 trade／carry／recent fill 三條歸屬規則與 `trades_range_uncovered` 這個 block，新 basis 不再寫 `accepted_capital_basis_credit_cell`。API 與營運 UI 不再有「未歸屬」的欄位；歸屬留下的兩個資料表欄位（`accepted_capital_basis_symbol.unattributed_credits`、`accepted_capital_basis_credit.attribution_basis`）以 migration 刪除（D1）。`cell_limit` 的公式（`max(0, T−R) × max_cell_fraction`）與 conservation 的判定不變。決定與理由見 ADR `docs/adr/2026-11-14-cell-limit-bounds-resting-offers-not-lent-stock.md`。可驗證的終態見步驟 5 與步驟 6 的驗收。

**上層：** 主清單在 repo 外，見本機個人脈絡檔 `AGENTS.local.md`。本 repo 沒有上層主計畫，下面的「全域約束與完成定義」暫代。

**附件：** `docs/plans/2026-11-14-cell-limit-resting-offers/`，快照 commit `9209abb1`（`dev`，2026-11-14）。

- `inventory.tsv`：會改到的每個機制一列（`basis.py` 逐段、讀取鏈、API、前端、還原演練、mutation gate、文件、測試），每列有「去向」（保留／改寫／刪除與理由）與負責的步驟。下文以列的 id（例如 B08）引用。
- `check_inventory.py`：每列每欄不得空白、去向以三個動詞之一開頭、步驟存在；並重跑搜尋，確認 `backend/src`、`backend/scripts`、`backend/tests`、`deploy`、`frontend/src`、`frontend/messages`、`frontend/e2e` 裡每個提到被改符號的檔都有列以完整路徑涵蓋（`modules/live_validation/` 與它的測試除外；`.github/` 與文件不含這些符號，靠 inventory 的 G02、D 列人工列出）。`python3 docs/plans/2026-11-14-cell-limit-resting-offers/check_inventory.py`（2026-11-14：`66 rows, 57 files named by the pattern, 0 errors`）。

## 全域約束與完成定義（暫代主計畫）

- 本計畫帶一個 Alembic migration（步驟 3，D1）。migration 只往前（`docs/adr/2026-10-08-forward-only-migrations.md`：`downgrade` 直接 raise），而且自己斷言資料（`docs/adr/2026-10-08-migrations-assert-their-data-and-deploy-rehearses-them.md`）；套用一律 `cd backend && uv run alembic upgrade head`。步驟 3 以外的任何一步發現需要改 schema，停下回報，不自行加 migration。`alembic check` 在每一步都必須無 drift。
- 放貸維持全自動：任何一步都不得把手動掛單、手動 kill 寫成正常流程（`AGENTS.md`「營運原則」）。
- `accepted_capital_basis*` 表 insert-once：不改、不刪既有列（刪欄是 DDL，不是改列）；`accepted_capital_basis_credit_cell` 留表（ADR Decision 3）。
- 週報的歸屬（`modules/live_validation/`）不動（ADR Decision 5）。
- 步驟 1、2、3、4 都進 `dev`，四者同一次 `dev`→`main` 發版（步驟 5）；步驟 2 併入後、步驟 4 併入前不發版。理由有二：`backend/ARCHITECTURE.md` 是架構的 source of truth，不能與程式不一致地上線；步驟 2 對兩個待刪欄位寫的是過渡值，只存在於 `dev`，到步驟 3 就連欄位一起消失，不進正式環境。
- 本機不跑 e2e 與 docker 測試；完整 integration、Playwright 與標 `docker` 的測試由 PR 的 CI 跑，CI 綠之前回報為待驗。
- 完成定義（每一步）：單元測試全過（`cd backend && uv run pytest -m "not integration" -n auto`）、受影響的 integration 檔本機通過且 PR 的 CI 全綠、`uv run mypy src/ && uv run ruff check`、import-linter 乾淨、`uv run alembic check` 無 drift、`uv run python scripts/quality_ratchet.py` 通過（測試數變少要有帶 `Ratchet-Reason:` 與 `Removed-Test:` trailer 的 commit）、改行為處附 mutation（在 repo 的複本裡跑，不在工作區改了再還原）、`/code-review` 跑過。

## 前提（開工時核對過，2026-11-14）

- P1 會改到的既有部署是否在線：**在線**。prod bot 在 VM 上跑，只在 owner 核准的 `dev`→`main` 發版時部署（`AGENTS.md`「部署架構」）。本計畫帶 migration（D1），所以：部署時先停 webapi 與 bot、備份、跑還原測試（含在還原副本上演練這個 migration），才對正式庫 migrate（`docs/runbooks/deploy.md` 第 92–94 行與「一次部署的步驟」）；bot 與 webapi 開機都比對 schema head、不符就拒絕（`core/schema_head.py`），所以**新舊版程式不會同時跑在同一個 schema 上，不需要相容性窗口**；**回滾＝往前修**（再發一個修正 release），不能部署上一版 digest，也不做 downgrade。資料搬移：沒有（只刪欄，不搬資料）。已確認（讀 runbook 與程式）。
- P2 ADR 已核可、status `active`（PR #395 已合併；`git log --oneline -1 -- docs/adr/2026-11-14-cell-limit-bounds-resting-offers-not-lent-stock.md` 為 `27920f28`）。已確認。
- P3 ADR 的盤點取自 `1b3b33b5`，之後 #394、#396 改過 execution 模組。2026-11-14 在 `9209abb1` 重跑 ADR 的搜尋，仍命中同樣 13 個檔；`git diff --stat 1b3b33b5 9209abb1 -- backend/src backend/scripts deploy frontend/src` 改到的 7 個檔（`command_gate.py`、`deployment/reconciler.py`、`managed_cancel.py`、`reservation_emitting.py`、`protocols.py`、`reconcile_monitors.py`、`observability/alerts.py`）都不在這 13 個裡。已確認（指令輸出）。
- P4 conservation 不依賴 `trades_range_uncovered`。`symbol_verdicts`（`ledger/_internal/conservation_facts.py` 第 99–251 行）的輸入沒有 block 清單與 cell 歸屬；它對 trades 範圍的要求是自己算的 `covers_lower`／`covers_upper`（第 142–144 行）。`rg -n trades_range_uncovered backend/src` 只命中 `basis.py` 第 418 行。已接受的 venue 觀測必有 `trades_complete`（`ledger/tables.py` 的 `ck_ledger_observation_acceptance`）。已確認（讀程式；步驟 2 的驗收再以 conservation 的整個測試檔確認）。
- P5 部分成交的證據（`basis.py` 的 `fills` 與 `_Fill`）只有 recent fill 規則一個讀者：`fills` 是 `write_basis` 的區域變數，只在第 435–445 行被讀。`conservation_facts.py` 裡同名的 `fills` 是它自己從 offer 列算的，不相干。已確認（讀程式）。
- P6 刪欄在這個 schema 上做得到的依據：(a) basis 表的不可變 trigger 是 `BEFORE UPDATE OR DELETE` 與 `BEFORE TRUNCATE`（`backend/alembic/versions/b1e2d3a4c5f6_add_ledger_s1_schema.py` 第 1314–1318 行），不擋 DDL；(b) PostgreSQL 刪欄時會一併刪掉提到該欄的 CHECK，所以 `ck_accepted_basis_symbol_amount`（它同時管 `available`、`offered`、`credits`、`foreign_offers`）要以原名重建成不含該欄的版本，`ck_accepted_basis_credit_attribution` 隨欄消失；(c) `bfx_webapi` 對 `unattributed_credits` 的欄位 SELECT 授權隨欄消失；(d) 還原演練的逐列雜湊用 `SELECT t.*`（`deploy/vm/pgbackrest/ledger_digest.py` 第 407 行），比對的兩邊在同一個 schema 上，不需要為刪欄改它，`rg -n 'unattributed|attribution' deploy/vm/pgbackrest` 0 行。(a)(d) 讀程式確認；(b)(c) 是 PostgreSQL 的既定行為，**未在本機實測**，由步驟 3 的 migration 測試與部署前的演練證明。
- P14 刪欄之後，v1 basis 的 `digest` 無法再由資料列重算（它的輸入含這兩欄的值）。沒有任何程式重算它（P9）。這是 D1 選 C 接受的後果。
- P7 「同一個 venue offer 有互相矛盾的 provenance 就擋住該幣別」這個檢查，現行由 `cell_of`（`basis.py` 第 163–170 行）順帶執行，reason 是 `credit_provenance_conflict`；對象是有成交的 terminal history 列、上一份 basis 有而現在不見的 offer、以及落在 active credit 組的 trade。ADR Decision 3 的刪除清單沒有提到它。現行沒有測試涵蓋這個 reason（`rg -n credit_provenance_conflict backend/tests` 0 行）。它在歸屬刪掉之後的去向是 D3。已確認（讀程式）。
- P8 還原演練的開機檢查（`apps/restore_boot_check.py` 第 89–93 行）依最新 basis 的 cell 列決定做哪些資金讀取。改動後錢全借出去、沒有掛單時 basis 沒有 cell 列，開機檢查就一次資金讀取都不做（drill 接受空的 `reads`：`deploy/vm/pgbackrest/ledger_digest.py` 第 600–601 行；既有測試 `tests/integration/test_ledger_restore_verification.py` 第 373–379 行釘住「沒有 cell 列就沒有讀取」）。要不要補上讀取是 D4。已確認（讀程式）。
- P9 `SCHEMA_VERSION`（`basis.py` 第 69 行）只有寫入者，沒有讀者：`rg -n 'schema_version|SCHEMA_VERSION' backend/src/bfx_funding_bot/modules/ledger backend/src/bfx_funding_bot/apps deploy/vm/pgbackrest` 在 basis 相關只命中寫入處與 CHECK（`schema_version >= 1`）。basis 的 `digest` 也沒有重算它的讀者（`rg -n 'bfx-ledger-capital-basis' backend deploy` 只命中 `basis.py` 第 581 行）。所以升版是改常數與 payload 形狀，不需要 migration（ADR Result 的「migration」那一列在收尾時更正）。已確認（指令輸出）。
- P10 告警定義在另一個 repo（見本機個人脈絡檔）。2026-11-14 在那裡搜尋 `trades_range_uncovered|unattributed|credit_provenance_conflict` 命中 0 個檔，本計畫不需要改告警。已確認（指令輸出）。
- P11 正式環境目前兩個 cell 同幣別（fUST）、同天期（2 天）（`backend/configs/cells.live.yaml`；ADR Context）。來源：ADR，2026-11-14；未重新核對設定檔以外的實際部署狀態。
- P12 模擬環境既有 basis 裡的舊歸屬列（ADR 記 9,042 筆多 cell 的 credit 列）：不處理。改動後沒有任何程式讀 `accepted_capital_basis_credit_cell`（步驟 2 刪掉唯一的讀取，inventory B04），那些列只是歷史。
- P13 `execution/venue_observation.py` 第 317–325 行在「還沒有任何已接受的觀測」時把 trades 的查詢起點往回推到每一筆 active credit 的 opening。它的註解寫明理由是歸屬（「carried … they need no trades」「Only with no accepted query yet must trades reach back to every current opening」）。ADR Decision 3 刪的是「三條歸屬規則與它們的輸入」，這段是 trade 規則的輸入，所以列入步驟 2（inventory O01）。第一份 basis 的 conservation 是 baseline、不看 trades（`conservation_facts.py` 第 117–118 行）。已確認（讀程式）。

**驗收基線：**（2026-11-14，`dev` 的 `9209abb1`）單元 `5384 passed`（本機，`uv run pytest -m "not integration" -n auto`）；完整 integration 與 Security 以 CI 為準：`main` 的 `498de711`（與 `dev` 內容相同，`git diff --stat origin/main origin/dev` 無輸出）CI 與 Security 都是 success。沒有既有失敗。

## 本階段依賴的不變量

| 不變量 | 依賴它的機制 |
|---|---|
| `E_cell` 的兩個來源都由 bot 自己的 provenance 精確決定：basis 的 cell 金額（掛著的受管 offer 的剩餘金額）＋尚未被 basis 反映的同 cell attempt | `ledger/_internal/basis.py` 的 `write_basis`（`cells` 的累計、`reflected_by_offer`）；`trading/capital.py` 的 `derive_capital`（`exposure`）；`ledger/read/provenance.py` 的 `offer_provenance`、`sole_owner` |
| `cell_limit`、`cell_headroom`、`max_new_offer` 的公式不變，planner、command gate、status 共用同一個 evaluator | `trading/policy.py` 的 `evaluate_capital`；`execution/deployment/sizing.py` 的 `allocate_capital`；`execution/safety/hard_guards.py`；`ledger/_internal/journal.py` 的預算重驗；`backend/scripts/capital_mutation_gate.py` 的 M1–M18 |
| 幣別的 `available`／`offered`／`credits`／`foreign_offers` 與 conservation 判定只來自 venue 的精確數字，與 cell 無關 | `ledger/_internal/conservation_facts.py` 的 `symbol_verdicts`；`ledger/conservation.py` 的 `conservation_verdict`；`accepted_capital_basis_symbol` 的兩個 CHECK |
| attempt 的分類（reflected／settled／unresolved／quarantined）不依賴 credit 的歸屬 | `basis.py` 的 `_reflected`、`_can_quarantine`、`_trades_cover`；`ledger/_internal/quarantine.py` 的 `open_quarantine` |
| basis 表的既有列 insert-once（刪的是欄，不是列）；`accepted_capital_basis_credit_cell` 留表 | `ledger/tables.py` 的 `LEDGER_TABLES`；`deploy/vm/pgbackrest/ledger_digest.py` 的 `RULES`；既有 migration 的欄位授權清單 |
| 舊 image 拒絕新 schema head；有 migration 的部署先停所有 writer、先在還原副本上演練 migration | `core/schema_head.py`、`apps/schema.py`；`deploy/vm/ops/bfx_deploy.py` 的停機與 migration 順序；`deploy/vm/pgbackrest/restore_drill.py` 的 `_rehearse` |
| 目標版 drill 能解析上一版與新版 image 的開機報告，逐列雜湊在新舊 basis 並存時相等 | `deploy/vm/pgbackrest/ledger_digest.py` 的 `parse_boot`、`_BOOT_READ_KEYS`、`RULES`；`apps/restore_boot_check.py` 的 `_reads` |
| 同一個 venue offer 的 provenance 互相矛盾時擋住該幣別（掛著的 offer 一律；已結束的 offer 依 D3） | `basis.py` 的 `_Classifier.owner`、`cell_of`；`ledger/read/provenance.py` 的 `sole_owner` |
| 每份 v2 basis 裡，每個幣別的 cell 金額加總等於該幣別的 `offered`（兩者都只由掛著的受管 offer 的剩餘金額累計） | `basis.py` 的 `write_basis`（`values.offered` 與 `cells` 的累計在同一處）；步驟 2 新增的測試；步驟 5 的正式環境查詢 |

## 範圍

- 做：`inventory.tsv` 裡去向為「改寫」「刪除」的每一列。
- 不做：
  - 刪 `accepted_capital_basis_credit_cell` 表或既有列（ADR Decision 3：留表與歷史）。
  - 改 `max_cell_fraction` 的值或 `cell_limit` 的公式（ADR Decision 1：公式不變）。
  - 改週報的歸屬（ADR Decision 5）。
  - 合併分支 `fix/basis-cell-attribution`（ADR Decision 3）；它的 worktree 與分支的清理列在步驟 6「需要人做的事」，因為刪遠端分支要 owner 同意。
  - 處理模擬環境的舊歸屬列（P12）。
- 延後：
  - 以 credit 自己的 `period` 計的天期上限（ADR Decision 6）。觸發條件：出現天期不同的 cell。

## 步驟

- [x] **1. 讀取鏈、API 與營運 UI 拿掉「未歸屬」**（被擋於：無）
  - 範圍：inventory R01–R03、R07–R10、A01–A06、F01–F05，以及 X03、X05 裡帶這些欄位的 fixture（F05 含 `frontend/e2e/uncertainty.spec.ts` 的 fixture）。`SymbolCapital.unattributed_credits`、`CapitalView.unattributed_credit_exposure`、`CapitalAvailable.unattributed_credit_exposure`、`PositionView.unattributed_lent`、funding status 的 `unattributed_credit_exposure`、positions 回應的 `unattributedLent`、前端兩處顯示與四則文案都刪除。`backend/openapi.json` 以 `cd backend && uv run python scripts/dump_openapi.py` 重新產生，`frontend/src/types/api.gen.ts` 以 `cd frontend && pnpm gen:api` 重新產生（不手改）。這一步之後 basis 仍照舊寫 `unattributed_credits` 欄，只是沒有人讀；欄位在步驟 3 刪除。
  - 消費端：`git grep -nE 'unattributed_credit|unattributed_lent|unattributedLent' -- backend/src backend/scripts backend/tests deploy frontend/src frontend/messages frontend/e2e backend/openapi.json`，文案鍵另以 `rg -n '"unattributed' frontend/messages` 找（2026-11-14 的結果在 inventory；`modules/live_validation/` 的命中是週報自己的計數，不動）。API 的呼叫端只有同一個 image 裡的前端（static export），沒有其他 repo 的消費端。
  - 不能動：`cell_limit`、`cell_headroom`、`max_cell_fraction`、`total_capital`、`spendable` 等其餘 API 欄位與它們的值；`basis.py`；資料表；`modules/live_validation/`。
  - 驗收：
    - 上面的搜尋在 `backend/src`、`frontend/src`、`frontend/messages`、`frontend/e2e`、`backend/openapi.json` 只剩 `ledger/_internal/basis.py`、`ledger/tables.py` 與 `modules/live_validation/` 的命中；`rg -n '"unattributed' frontend/messages` 0 行。
    - `cd backend && uv run pytest -m "not integration" -n auto` 全過；本機跑 `tests/integration/test_ledger_capital_reader.py`、`test_ledger_positions_offers.py`、`contracts/test_capital_authority_contract.py`；`cd frontend && pnpm test && pnpm lint`（Vitest 與 `tsc`＋biome；照 `frontend/AGENTS.md`）。
    - API 契約：CI 的契約檢查（`ci.yml` 重新產生後 `git diff --exit-code -- backend/openapi.json frontend/src/types/api.gen.ts`）通過；`tests/modules/api/test_response_contract.py` 通過。
    - 刪掉的測試（`positions-card.test.tsx` 的兩條 `unattributedLent` 顯示測試）列在 commit 的 `Removed-Test:`，並附 `Ratchet-Reason:`；`quality_ratchet.py` 通過。
    - 這一步是刪除欄位，沒有新行為可以 mutation；以型別檢查（`mypy`、前端的型別檢查）證明沒有殘留的讀者。
  - 停止條件：搜尋出現 inventory 沒有列到的讀者（例如另一個腳本或排程讀這些 API 欄位）；`alembic check` 出現 drift。
- [ ] **2. basis 不再把 credit 歸屬到 cell**（被擋於：步驟 1）
  - 範圍：inventory B01–B18、O01、T01–T04（只確認不動）、R02 的 docstring、R04–R06 的 docstring、V01、V02、G01、G02、X01–X05、X07–X10、O02、D05。重點：
    - `write_basis`：`cells` 只由掛著的受管 offer 的剩餘金額累計；刪掉三條歸屬規則與它們的輸入、`trades_range_uncovered`、credit_cell 的寫入與對上一份 basis 的 credit_cell 讀取；payload 升為 v2（`SCHEMA_VERSION = 2`，symbols 列不含 unattributed，credits 列不含 attribution 與 cell 集合）；`unattributed_credits` 與 `attribution_basis` 兩欄在這一步寫過渡值（`unattributed_credits = credits`、`attribution_basis = 'unattributed'`，兩者都滿足既有 CHECK），資料表不動；欄位與寫入在步驟 3 一起刪。
    - 已結束的 offer 的 provenance 矛盾檢查照 D3。
    - `venue_observation.py` 第 317–325 行：刪掉「沒有已接受的觀測時把 trades 起點推回每筆 credit 的 opening」（O01、P13）；`tests/modules/execution/test_venue_observation.py` 第 221–250 行的三條測試照 inventory X08 處理；刪掉之後 `ObservationWindow.previous_query_started_at_ms` 在 `backend/src` 沒有讀者，欄位一併刪除（inventory O02）。
    - `restore_boot_check.py` 與 `test_ledger_restore_verification.py` 第 373–379 行照 D4（inventory V01、V02、X07）；`docs/runbooks/offsite-dr.md` 第 519–521 行對開機檢查的描述一併改（inventory D05）。
    - `capital_mutation_gate.py` 新增 integration 的 mutant M19「basis 把借出的 credit 計入 cell」：錨點是改寫後 credit 迴圈裡累計 `values.credits` 的那一行（在檔案裡要恰好出現一次），替換成同一行再加上「把這筆 credit 的金額加進該幣別每一個已有金額的 cell」；owner 是 `tests/integration/test_ledger_basis.py`。檔頭的 Rule ownership 清單加一行；gate 自己的測試 `tests/scripts/test_capital_mutation_gate.py` 第 22 行把 mutant 清單釘在 M1–M18，改成到 M19（inventory X10）。`.github/workflows/capital-rules.yml` 的 `paths` 與判斷要不要跑 integration mutant 的 `pattern` 都加上 `ledger/_internal/basis.py` 與 `tests/integration/test_ledger_basis.py`（inventory G02），這樣只改 `basis.py` 的 PR 也會跑 M19。
    - 測試：`test_ledger_basis.py` 第 397–592 行的 9 條歸屬測試逐條處理——描述新定義仍成立的事實的（例如部分成交後 cell 只剩掛著的剩餘金額、外來 offer 不計入）改寫；只描述被刪規則的刪除並列入 `Removed-Test:`。新增至少這幾條：借出的 credit 不計入任何 cell（同時有掛單與借出時，cell 金額只等於掛單的剩餘金額；M19 靠它擋下）；每個幣別的 cell 金額加總等於 `offered`；兩個 cell 在同一個 opening 各成交後，各自的 cell 金額只等於各自還掛著的剩餘金額；新 basis 沒有 credit_cell 列；新 opening 沒有 trades 涵蓋時幣別不被擋；D3 與 D4 的結論各一條（D3：已結束的 offer 的 provenance 矛盾時幣別被擋；D4：錢全借出去時開機檢查仍對每個 symbol 做一次讀取）。
  - 消費端：`git grep -nE 'cell_exposure|\.cells\b|accepted_capital_basis_(credit_)?cell|AcceptedCapitalBasis(Credit)?Cell|trades_range_uncovered|credit_provenance_conflict|attribution_basis' -- backend/src backend/scripts backend/tests deploy`；trades 起點另以 `rg -n 'trade_start_ms|previous_query_started_at_ms' backend/src backend/tests` 找。block 的 reason 字串另有 repo 外的告警定義，見 P10（0 命中）。
  - 不能動：`trading/policy.py` 的 `evaluate_capital` 與 `execution/deployment/sizing.py` 的程式（只改 docstring）；`conservation_facts.py`、`conservation.py`；`_reflected`、`_can_quarantine`、`_trades_cover`；資料表定義與既有 migration；`ledger_digest.py` 的程式（只改第 600–601 行的註解）；既有 mutant M1–M18 的錨點。
  - 驗收：
    - `python3 docs/plans/2026-11-14-cell-limit-resting-offers/check_inventory.py` 0 錯誤。
    - `rg -n 'recent_fill|prior_cells|carried|traders|_Fill|trades_range_uncovered|AcceptedCapitalBasisCreditCellRow' backend/src/bfx_funding_bot/modules/ledger/_internal/basis.py` 0 行。
    - 單元全過；本機跑 `tests/integration/test_ledger_basis.py`、`test_ledger_conservation.py`（整檔，確認 P4）、`test_ledger_observation_window.py`、`test_ledger_capital_reader.py`、`test_ledger_restore_verification.py`、`contracts/test_capital_authority_contract.py`；`uv run alembic check` 無 drift。
    - mutation：gate 以外的每一條都在 repo 的複本裡做（`backend/scripts/mutation_sandbox.py` 的 `copy_repository` 做複本，gate 用的就是它；或 `git worktree add` 一個拋棄式 worktree），不在工作區改了再還原；每一條把指令與 pytest 的原始輸出貼進進度表的「已跑的驗收」欄。
      - 改回舊寫法要失敗：把 credit 金額加回 cell → 「借出的 credit 不計入任何 cell」那條測試失敗（也就是新增的 gate mutant；`cd backend && uv run python -m scripts.capital_mutation_gate --include-integration` 全部 killed）。
      - 把 `trades_range_uncovered` 的 block 加回去 → 「新 opening 沒有 trades 涵蓋時幣別不被擋」失敗。
      - 拿掉 D3 保留的檢查 → provenance 矛盾那條測試失敗。
      - 把開機檢查改回只依 cell 列 → 「錢全借出去仍有讀取」那條測試失敗。
      - 把 `venue_observation.py` 往回推 opening 的邏輯加回去 → `test_venue_observation.py` 改寫後的第 221 行那條失敗。
    - 還原演練的新舊方向：`tests/integration/test_ledger_restore_verification.py` 通過（第 373–379 行那條已照 D4 改寫）；另以一條測試確認同一個庫裡 v1 basis（有 credit_cell 列）之後接 v2 basis（沒有）時，`ledger_digest` 的逐列雜湊在副本與來源相等。
    - 刪掉的測試逐條列在 `Removed-Test:`，附 `Ratchet-Reason:`；`quality_ratchet.py` 在新測試檔 commit 之後跑。
  - 停止條件：conservation 的任何測試在不改它的情況下失敗（P4 不成立）；發現 `fills`、`trades_range_uncovered` 或 credit_cell 列有 inventory 沒列到的讀者；這一步需要改資料表才能完成（資料表的改動屬於步驟 3）。
  - PR 大小：這一步的 diff 可能超過 `backend/scripts/pr_size.py` 的 1000 行上限。超過時先把開機檢查（V01、V02、X07）拆成獨立的 PR；仍超過才在 PR body 加 `Size-Justification:`。
  - 完成後的狀態：`backend/ARCHITECTURE.md` 的 I-CAP、I-CC 與程式不一致，直到步驟 4 併入；兩個待刪欄位寫的是過渡值，直到步驟 3 併入。步驟 4 併入前不發版。
- [ ] **3. 刪掉 `unattributed_credits` 與 `attribution_basis` 兩欄**（被擋於：步驟 1、步驟 2）
  - 範圍：inventory M01、T01、T02、B14、B15、X11。
    - 新 migration（`backend/alembic/versions/` 新檔，`down_revision` 接當時的 head；2026-11-14 的 head 是 `545b2877e9bd`）：刪 `accepted_capital_basis_symbol.unattributed_credits` 與 `accepted_capital_basis_credit.attribution_basis`；以原名重建 `ck_accepted_basis_symbol_amount`（`available >= 0 AND offered >= 0 AND credits >= 0 AND foreign_offers >= 0`）。
    - migration 自己斷言資料：刪欄前後兩張表的列數相同；刪欄後兩欄在 `information_schema.columns` 不存在；`bfx_webapi` 對 `accepted_capital_basis_symbol` 的欄位 SELECT 授權恰好剩 `basis_id`、`symbol`、`available`、`offered`、`credits`。`downgrade` 照 forward-only 的 ADR 直接 raise。`ledger_contract` 照 `core/schema_head.py` 的說明宣告（預期是 `preserved`：不動 projector cursor 與 projection archive；執行時讀該檔確認，`tests/test_schema_head.py` 會擋沒宣告的 migration）。
    - `ledger/tables.py` 拿掉兩個欄位與 `ck_accepted_basis_credit_attribution`，`ck_accepted_basis_symbol_amount` 改成新的式子；`basis.py` 拿掉步驟 2 的兩個過渡寫入。
    - 測試：`tests/integration/test_ledger_schema_roles.py` 第 407、425、1004、1202 行的原生 SQL、`test_ledger_webapi_grants.py` 第 70 行與 `test_webapi_privilege_allowlist.py` 第 117、208 行的欄位清單、`test_ledger_basis.py` 裡讀這兩欄的斷言與 helper，都改成沒有這兩欄。`test_ledger_conservation_migration.py` 第 48 行的 SQL 跑在較早的 revision 上，不動。新增一條 migration 測試：在上一個 revision 的庫裡放有值的列，升到 head 後列數不變、兩欄不存在、CHECK 仍擋負數、webapi 的授權如上。
  - 消費端：`git grep -nE 'unattributed_credits|attribution_basis' -- backend/src backend/scripts backend/tests deploy`（既有 migration 檔裡的欄位清單是歷史，不改；`modules/live_validation/` 的同名計數不相干）。
  - 不能動：既有 migration 檔；`accepted_capital_basis_credit_cell` 表；其他欄位與 CHECK；`ledger_digest.py`；任何既有列的內容。
  - 驗收：
    - `cd backend && uv run alembic upgrade head && uv run alembic check` 無 drift；`tests/test_schema_head.py` 通過。
    - 消費端的搜尋在 `backend/src` 只剩 `modules/live_validation/`；在 `backend/alembic/versions` 只多出新 migration。
    - 單元全過；本機跑新的 migration 測試、`test_ledger_schema_roles.py`、`test_ledger_webapi_grants.py`、`test_webapi_privilege_allowlist.py`、`test_ledger_basis.py`、`test_ledger_capital_reader.py`、`test_ledger_restore_verification.py`。
    - mutation（在 repo 的複本裡做，輸出貼進進度表）：migration 裡不重建 `ck_accepted_basis_symbol_amount` → migration 測試的「CHECK 仍擋負數」失敗；migration 少刪一欄 → 「兩欄不存在」失敗。
    - `quality_ratchet.py` 通過。
  - 停止條件：刪欄被 PostgreSQL 以「other objects depend on it」拒絕（有 inventory 沒列到的依賴：view、trigger function、index）；`ledger_contract` 判斷不出來；還原演練的測試在不改 `ledger_digest.py` 的情況下失敗（P6(d) 不成立）。
  - 完成後的狀態：`dev` 帶著未發版的 migration；之後進 `dev` 的其他 migration 要接在它後面。
- [ ] **4. 架構文件改寫**（被擋於：步驟 3）
  - 範圍：inventory D01、D03。`backend/ARCHITECTURE.md` 第 267 行（資金預算段的「歸屬該 cell 的 active credits」）、第 281 行（acceptance 存進 basis 的「credit 的 cell 歸屬」）、第 340 行（「cell exposure 只算可歸屬的部分」）、第 501 行（basis 表清單）、第 872 行（I-CAP 的 `E_cell` 定義與歸屬規則）、第 874 行（I-CC）改成 ADR Decision 1–3 的定義；寫明 `accepted_capital_basis_credit_cell` 是留著的歷史表、新 basis 不寫入，以及 `unattributed_credits`／`attribution_basis` 兩欄已刪除（D1）。純文件，不加測試。
  - 消費端：`rg -n 'E_cell|I-CAP|I-CC|credit_cell|unattributed|trades_range_uncovered|recent fill|歸屬' backend/ARCHITECTURE.md docs/runbooks docs/slo.md`。
  - 不能動：ADR 的 Context／Decision／Rationale；其他不相干的不變量段落。
  - 驗收：把上面的搜尋在 `backend/ARCHITECTURE.md` 的每一個命中行列在 PR 說明裡，逐行寫明「已改」或「可留與原因」（例如講週報歸屬或外來 offer 的行）；沒有任何一行還把「歸屬到 cell 的 active credits」「trade／carry／recent fill」「`trades_range_uncovered`」寫成現行行為；文件裡提到的每個符號與路徑都以 `rg` 在程式裡找得到。
- [ ] **5. 發版與正式環境驗證**（被擋於：步驟 1、2、3、4；owner 核准發版）
  - 範圍：開 `dev`→`main` 的發版 PR（merge commit），CI 與 Security 綠之後由 owner 核准合併；`bfx-deploy` 以 digest 部署。這次發版帶 migration，所以 `bfx-deploy` 會先停 webapi 與 bot、備份、跑還原測試（在還原副本上演練這個 migration 並跑候選 image 的開機檢查），通過才對正式庫 migrate；還原測試或演練失敗時正式庫還沒動，部署不會開始。停機時間由演練量出，部署前回報。其餘照 `docs/runbooks/deploy.md`。
  - 預期的行為變化（寫進發版 PR 的說明）：部署後第一份 basis 起，每個 cell 的 `E_cell` 少掉原本歸屬給它的 credit，headroom 變大；bot 會在 `spendable` 與 `cell_limit` 允許的範圍內補掛單。總額、保留、單筆上限、天期、利率下限、掛單數這些包絡不變。
  - 驗收（正式環境，唯讀查詢）：
    - 部署成功：`readlink /home/ubuntu/bfx-releases/current` 指向新 release，`journalctl -u bfx-deploy` 該次無錯誤。
    - schema：`alembic_version` 是新 migration 的 revision；`information_schema.columns` 裡 `accepted_capital_basis_symbol.unattributed_credits` 與 `accepted_capital_basis_credit.attribution_basis` 不存在；兩張表的列數不少於部署前（部署前先記下）。
    - 部署後的最新 basis：`schema_version = 2`；`accepted_capital_basis_credit_cell` 沒有該 basis 的列；每個 symbol 的 `accepted_capital_basis_cell` 金額加總等於 `accepted_capital_basis_symbol.offered`；symbol 列的 `block` 不含 `trades_range_uncovered`，`conservation` 不是 `unexplained_lending`。
    - 部署後 30 分鐘的 bot log 沒有錯誤：`docker logs --since <部署完成時間> bfx-bot 2>&1 | grep -ciE '"level": "(error|critical)"|Traceback'` 為 0。執行前先用 `docker logs --tail 5 bfx-bot` 確認 log 仍是這個 JSON 格式；格式不同就改用對應的寫法並記在進度表。
    - API 不再有「未歸屬」欄位：由發版 commit 的 CI 契約檢查證明（image 由同一個 commit 建），不另外在正式環境打 API。
    - 還原演練通過（新舊 basis 並存時逐列雜湊相等；開機檢查對每個 symbol 有讀取）：排程是每月 1 日 09:17 UTC（`docs/runbooks/operations.md` §8 的 `bfx-restore-test.timer`），不等它；部署後、至少有一份 `schema_version = 2` 的 basis 進了備份之後，在 VM 手動啟動一次 `sudo systemctl start bfx-restore-test@current`，以 `journalctl -u 'bfx-restore-test@current'` 與演練的 receipt 讀結果，並確認 receipt 的 bounds（`observation_query_revision`）不小於第一份 v2 basis 的 observation 的 query revision；不滿足就等下一次備份再跑。這和部署前那一次不同：部署前還原的資料只有 v1 basis、舊 schema。目標標籤早於這次 migration 的演練會先被開機檢查以 `boot_schema_head_mismatch` 拒絕，任何帶 migration 的發版都一樣。沒跑之前回報為待驗。
  - 回滾：不能部署上一版 digest（上一版 image 在新 schema head 上拒絕開機），也不 downgrade。migration 之前失敗（還原測試、演練）：正式庫沒動，修好再發。migration 之後出問題：先以 `sudo bfx-op` 的既有控制停掉放貸（緊急控制，不是正常流程），再發修正 release 往前修。觸發條件：部署後最新 basis 出現新的 fact block、`conservation` 轉為 `unexplained_lending`、或 bot 開機失敗。
  - 停止條件：上面任何一條驗收不成立。先停放貸並回報，再決定修正 release 的內容。
  - 需要人做的事：owner 核准並合併發版 PR；VM 的唯讀查詢與手動啟動還原演練需要 owner 通過 Tailscale SSH 的瀏覽器驗證。
- [ ] **6. 收尾**（被擋於：步驟 5）
  - 完成定義逐項核對（上面的清單，逐步驟）。
  - 獨立設計審查：對 `dev` 上步驟 1–4 的累積 diff 跑 design-review，檢查有沒有沒重新評估就沿用的機制、繞著既有結構的補丁、留成 pending 的項目。
  - ADR 的 Result 段換成實際結果（inventory D02），包含：`schema_version` 升版本身不需要 migration（P9）、兩個欄位以 migration 刪除與 v1 digest 不能重算（D1、P14）、D2–D4 的結論、`venue_observation.py` 的 trades 起點（P13）。在 `docs/adr/2026-10-02-ledger-s1-port-and-wire-contracts.md` 第 83 行的 positions wire 契約標註 `unattributedLent` 由本 ADR 取代（inventory D04）。
  - `check_inventory.py` 是只在本階段有意義的一次性檢查，留在附件，不接進 CI；長期守住新行為的是新增的 gate mutant M19（PR 上由 `capital-rules.yml` 的路徑觸發，另有每晚排程）與測試。
  - 延後項（上面「延後」一條）附觸發條件搬回主清單。
  - 需要人做的事：分支 `fix/basis-cell-attribution` 與它的 worktree 是否刪除（刪遠端分支要 owner 同意）；本機個人脈絡檔「進行中的計畫」那一行更新。
  - 驗收：`python3 docs/plans/2026-11-14-cell-limit-resting-offers/check_inventory.py`；設計審查的報告與處理結果寫在進度表；ADR 的 Result 段不再有「尚未實作」。

## 決定

- **D1 `unattributed_credits` 與 `attribution_basis` 兩個欄位的去向**（狀態：已決 2026-11-14，Will；擋住步驟：2、3）
  - A：留欄，新列寫固定值——不帶 migration，可以用上一版 digest 回滾；留下兩個從此不變的欄位。
  - B：兩欄改成可為 NULL，新列寫 NULL——要 migration，回滾只能往前修。
  - C：刪掉兩欄——要 migration，回滾只能往前修；舊 basis 的歸屬紀錄（這兩欄的值）消失，v1 的 digest 不能再重算（P14）。
  - 結論：C。步驟 2 先寫過渡值，步驟 3 以 migration 刪欄，同一次發版。ADR：收尾時寫進 `docs/adr/2026-11-14-cell-limit-bounds-resting-offers-not-lent-stock.md` 的 Result。
- **D2 API 與營運 UI 的「未歸屬」欄位：刪除還是改名**（狀態：已決 2026-11-14，Will；擋住步驟：1）
  - 背景：ADR Decision 4 寫「改成不顯示或改名，另列在實作計畫」。`unattributedLent` 是 `docs/adr/2026-10-02-ledger-s1-port-and-wire-contracts.md` 第 83 行拍板的 positions wire 契約的一部分（「`lent` 的子集合、不參與加總」），刪除等於修訂那一段。funding status 的 `unattributed_credit_exposure` 與 positions 的 `unattributedLent` 在改動後恆等於該幣別的借出總額（positions 已有 `lent`）。
  - A：刪除兩個 API 欄位、前端兩處顯示與文案——API 的消費端只有同一個 image 裡的前端，同一次發版一起換，沒有相容性問題。（建議）
  - B：改名保留（例如改叫「借出中、不計入 cell 上限」）——畫面多一個與 `lent` 相同的數字。
  - 結論：A，刪除。

- **D3 已結束的 offer 的 provenance 矛盾檢查：歸屬刪掉之後留不留**（狀態：已決 2026-11-14，Will；擋住步驟：2）
  - 背景：見 P7。這個檢查是歸屬程式順帶做的；掛著的 offer 有自己的同類檢查（`offer_provenance_conflict`），不受影響。
  - A：保留，對象是有成交的 terminal history 列、上一份 basis 有而現在不見的 offer、本次觀測的每一筆 trade，reason 字串不變——兩個 attempt 都宣稱同一個 venue offer 是帳本層的矛盾，與 cell 無關；`_reflected` 會用 terminal 列把 attempt 判成已反映，矛盾時不擋可能讓兩個 attempt 都被判成已反映。對象比現行多出「trade 對應的 credit 已結清」的情形（現行靠 credit 分組篩掉，分組會被刪），也就是多一種會擋住該幣別的情況。補一條測試。（建議）
  - B：保留，但對象只有 terminal history 列與不見的 offer，不看 trade——不會比現行多擋，但少了「只在 trade 裡看得到的 offer」這一類。
  - C：連同歸屬一起刪——最少程式，放寬了一個現行會擋的情況；ADR 沒有授權這個放寬。
  - 結論：A，保留，對象含本次觀測的每一筆 trade。
- **D4 還原演練的開機檢查在「沒有 cell 列」時要不要仍做資金讀取**（狀態：已決 2026-11-14，Will；擋住步驟：2）
  - 背景：見 P8。現行「沒有 cell 列」只發生在帳戶沒有掛單也沒有被歸屬的借出；改動後「錢全借出去、沒有掛單」也會沒有 cell 列，而這在正式環境是常態。
  - A：每個 symbol 列至少讀一次，沒有 cell 列時用固定的探測 cell id `boot-check`——開機檢查在常態下仍會實際走一次資金讀取；報告的鍵不變（`cell_id` 是字串），目標版 drill 解析上一版與新版 image 的報告都不必改程式；報告裡會出現一個不是真實 cell 的 id；要改一條既有測試與 `ledger_digest.py` 的註解（後者讓這次發版在部署前多跑一次還原測試）。（建議）
  - B：維持現狀，沒有 cell 列就不讀——不改程式；但開機檢查在常態下不再驗證資金讀取，演練只剩 schema、realm、authority 與逐列雜湊。
  - 結論：A，每個 symbol 至少讀一次，探測 cell id 為 `boot-check`。

拍板順序：四題都在 2026-11-14 由 Will 一次拍板（D1：C；D2：A；D3：A；D4：A），沒有未決的題目。

## 進度

狀態值：未開始／進行中／程式完成待驗收／完成。

| 步驟 | 狀態 | 已跑的驗收 | 未跑的驗收與原因 | commit |
|---|---|---|---|---|
| 1 讀取鏈、API 與營運 UI 拿掉「未歸屬」 | 完成 | 2026-11-14：消費端搜尋只剩 `basis.py`、`tables.py`、`modules/live_validation/`，文案鍵 0 行；本機 `mypy`、`ruff`、`lint-imports --no-cache`（26 kept）、三個受影響的 integration 檔（53 passed）、`pnpm lint`、`pnpm test`（167 passed）、`quality_ratchet.py`；本機單元 5383 passed、1 failed（`test_tick_candles.py::test_a_venue_that_never_answers_does_not_stall_the_tick`，單獨跑通過，CI 的 unit job 通過）；PR #400 的 CI 與 Security 全綠（unit、integration、Frontend、e2e smoke、契約檢查、Quality ratchet、Capital mutation gate）；`test_ledger_positions_offers.py` 檔頭的 mutation（`lent = credits + offered`）在複本實跑，被 `test_positions_use_only_the_latest_basis` 擋下；`/code-review`：findings 7（blocking 0），fixed 3、recorded 2、rejected 2、raised 0；`tests_executed` 5862→5860（兩條 `Removed-Test:`） | 無 | `e87b106d`（PR #400） |
| 2 basis 不再把 credit 歸屬到 cell | 未開始 | | | |
| 3 刪掉 `unattributed_credits` 與 `attribution_basis` 兩欄 | 未開始 | | | |
| 4 架構文件改寫 | 未開始 | | | |
| 5 發版與正式環境驗證 | 未開始 | | | |
| 6 收尾 | 未開始 | | | |
