# cell 上限只管掛單中的金額：實作計畫

**Goal：** 每份新寫的 capital basis 裡，`accepted_capital_basis_cell` 的金額只等於該 cell 掛著的受管 offer 的剩餘金額；已借出的 credit 只計入幣別的 `credits` 與總量 `T`，不計入任何 cell。`basis.py` 不再有 trade／carry／recent fill 三條歸屬規則與 `trades_range_uncovered` 這個 block，新 basis 不再寫 `accepted_capital_basis_credit_cell`。API 與營運 UI 不再有「未歸屬」的欄位；歸屬留下的兩個資料表欄位（`accepted_capital_basis_symbol.unattributed_credits`、`accepted_capital_basis_credit.attribution_basis`）以 migration 刪除（D1）。`cell_limit` 的公式（`max(0, T−R) × max_cell_fraction`）與 conservation 的判定不變。決定與理由見 ADR `docs/adr/2026-11-14-cell-limit-bounds-resting-offers-not-lent-stock.md`。可驗證的終態見步驟 5、5b 與步驟 6 的驗收。

**上層：** 主清單在 repo 外，見本機個人脈絡檔 `AGENTS.local.md`。本 repo 沒有上層主計畫，下面的「全域約束與完成定義」暫代。

**附件：** `docs/plans/2026-11-14-cell-limit-resting-offers/`，快照 commit `9209abb1`（`dev`，2026-11-14）。

- `inventory.tsv`：會改到的每個機制一列（`basis.py` 逐段、讀取鏈、API、前端、還原演練、mutation gate、文件、測試），每列有「去向」（保留／改寫／刪除與理由）與負責的步驟。下文以列的 id（例如 B08）引用。
- `check_inventory.py`：每列每欄不得空白、去向以三個動詞之一開頭、步驟存在；並重跑搜尋，確認 `backend/src`、`backend/scripts`、`backend/tests`、`deploy`、`frontend/src`、`frontend/messages`、`frontend/e2e` 裡每個提到被改符號的檔都有列以完整路徑涵蓋（`modules/live_validation/` 與它的測試除外；`.github/` 與文件不含這些符號，靠 inventory 的 G02、D 列人工列出）。`python3 docs/plans/2026-11-14-cell-limit-resting-offers/check_inventory.py`（2026-11-14：`66 rows, 57 files named by the pattern, 0 errors`）。

## 全域約束與完成定義（暫代主計畫）

- 本計畫帶一個 Alembic migration（步驟 3，D1）。migration 只往前（`docs/adr/2026-10-08-forward-only-migrations.md`：`downgrade` 直接 raise），而且自己斷言資料（`docs/adr/2026-10-08-migrations-assert-their-data-and-deploy-rehearses-them.md`）；套用一律 `cd backend && uv run alembic upgrade head`。步驟 3 以外的任何一步發現需要改 schema，停下回報，不自行加 migration。`alembic check` 在每一步都必須無 drift。
- 放貸維持全自動：任何一步都不得把手動掛單、手動 kill 寫成正常流程（`AGENTS.md`「營運原則」）。
- `accepted_capital_basis*` 表 insert-once：不改、不刪既有列（刪欄是 DDL，不是改列）；`accepted_capital_basis_credit_cell` 留表（ADR Decision 3）。
- 週報的歸屬（`modules/live_validation/`）不動（ADR Decision 5）。
- 發版分兩次（D8）。第一次：步驟 1、2、4 進 `dev` 後一起發（步驟 5），不帶 migration；步驟 2 併入後、步驟 4 併入前不發版，因為 `backend/ARCHITECTURE.md` 是架構的 source of truth，不能與程式不一致地上線。第二次：步驟 5 的驗收通過、觀察期滿之後，步驟 3 的 PR 才合併進 `dev` 並發版（步驟 5b）；在那之前 `dev` 不帶這支 migration，步驟 2 對兩個待刪欄位寫的過渡值會在正式環境存在一個 release。
- 本機不跑 e2e 與 docker 測試；完整 integration、Playwright 與標 `docker` 的測試由 PR 的 CI 跑，CI 綠之前回報為待驗。
- 完成定義（每一步）：單元測試全過（`cd backend && uv run pytest -m "not integration" -n auto`）、受影響的 integration 檔本機通過且 PR 的 CI 全綠、`uv run mypy src/ && uv run ruff check`、import-linter 乾淨、`uv run alembic check` 無 drift、`uv run python scripts/quality_ratchet.py` 通過（測試數變少要有帶 `Ratchet-Reason:` 與 `Removed-Test:` trailer 的 commit）、改行為處附 mutation（在 repo 的複本裡跑，不在工作區改了再還原）、`/code-review` 跑過。

## 前提（開工時核對過，2026-11-14）

- P1 會改到的既有部署是否在線：**在線**。prod bot 在 VM 上跑，只在 owner 核准的 `dev`→`main` 發版時部署（`AGENTS.md`「部署架構」）。本計畫帶 migration（D1），所以：部署時先停 webapi 與 bot、備份、跑還原測試（含在還原副本上演練這個 migration），才對正式庫 migrate（`docs/runbooks/deploy.md` 第 92–94 行與「一次部署的步驟」）；bot 與 webapi 開機都比對 schema head、不符就拒絕（`core/schema_head.py`），所以**新舊版程式不會同時跑在同一個 schema 上，不需要相容性窗口**；**回滾＝往前修**（再發一個修正 release），不能部署上一版 digest，也不做 downgrade。資料搬移：沒有（只刪欄，不搬資料）。已確認（讀 runbook 與程式）。
- P2 ADR 已核可、status `active`（PR #395 已合併；`git log --oneline -1 -- docs/adr/2026-11-14-cell-limit-bounds-resting-offers-not-lent-stock.md` 為 `27920f28`）。已確認。
- P3 ADR 的盤點取自 `1b3b33b5`，之後 #394、#396 改過 execution 模組。2026-11-14 在 `9209abb1` 重跑 ADR 的搜尋，仍命中同樣 13 個檔；`git diff --stat 1b3b33b5 9209abb1 -- backend/src backend/scripts deploy frontend/src` 改到的 7 個檔（`command_gate.py`、`deployment/reconciler.py`、`managed_cancel.py`、`reservation_emitting.py`、`protocols.py`、`reconcile_monitors.py`、`observability/alerts.py`）都不在這 13 個裡。已確認（指令輸出）。
- P4 conservation 不依賴 `trades_range_uncovered`。`symbol_verdicts`（`ledger/_internal/conservation_facts.py` 第 99–251 行）的輸入沒有 block 清單與 cell 歸屬；它對 trades 範圍的要求是自己算的 `covers_lower`／`covers_upper`（第 142–144 行）。`rg -n trades_range_uncovered backend/src` 只命中 `basis.py` 第 418 行。已接受的 venue 觀測必有 `trades_complete`（`ledger/tables.py` 的 `ck_ledger_observation_acceptance`）。已確認（讀程式；步驟 2 的驗收再以 conservation 的整個測試檔確認）。
- P5 部分成交的證據（`basis.py` 的 `fills` 與 `_Fill`）只有 recent fill 規則一個讀者：`fills` 是 `write_basis` 的區域變數，只在第 435–445 行被讀。`conservation_facts.py` 裡同名的 `fills` 是它自己從 offer 列算的，不相干。已確認（讀程式）。
- P6 刪欄在這個 schema 上做得到的依據：(a) basis 表的不可變 trigger 是 `BEFORE UPDATE OR DELETE` 與 `BEFORE TRUNCATE`（`backend/alembic/versions/b1e2d3a4c5f6_add_ledger_s1_schema.py` 第 1314–1318 行），不擋 DDL；(b) PostgreSQL 刪欄時會一併刪掉提到該欄的 CHECK，所以 `ck_accepted_basis_symbol_amount`（它同時管 `available`、`offered`、`credits`、`foreign_offers`）要以原名重建成不含該欄的版本，`ck_accepted_basis_credit_attribution` 隨欄消失；(c) `bfx_webapi` 對 `unattributed_credits` 的欄位 SELECT 授權隨欄消失；(d) 還原演練的逐列雜湊用 `SELECT t.*`（`deploy/vm/pgbackrest/ledger_digest.py` 第 407 行），比對的兩邊在同一個 schema 上，不需要為刪欄改它，`rg -n 'unattributed|attribution' deploy/vm/pgbackrest` 0 行。(a)(d) 讀程式確認。(b) 不再依賴：步驟 3 的 migration 先以名稱刪掉兩個 CHECK 再刪欄（與 `545b2877e9bd` 的寫法一致），不靠 PostgreSQL 連帶刪除。(c) 已確認（2026-11-14，步驟 3 的 migration 測試：刪欄後 `bfx_webapi` 可讀的欄位恰好少掉被刪的那一欄）。
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
  - `test_ledger_schema_roles.py` 的 `_seed` 同時用在 head 與 `LAST_REVERSIBLE_REVISION`，每刪一次欄就多一個「看資料庫有沒有這欄」的分支（現在兩個：realm、歸屬欄）。觸發條件：下一支 migration 再改 `_seed` 寫到的表的形狀時，把較早 revision 用的 seed 拆出去凍結，不再加第三個探測；`test_drop_basis_attribution_columns.py` 也用 `_seed`（落在 `545b2877e9bd`），拆的時候一起給它一份凍結的 seed（步驟 3 兩次設計審查的發現）。
  - basis 的 `digest` 沒有任何程式重算它（P9）。要不要補「由觀測列重算並比對」的檢查，或之後刪掉欄位。觸發條件：需要稽核某份 basis 是否可由它的觀測重算，或下一次改 basis payload 的形狀。

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
- [x] **2. basis 不再把 credit 歸屬到 cell**（被擋於：步驟 1）
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
- [ ] **3. 刪掉 `unattributed_credits` 與 `attribution_basis` 兩欄**（被擋於：步驟 1、步驟 2；PR 合併進 `dev` 被擋於步驟 5 的驗收與觀察期，D8）
  - 範圍：inventory M01、T01、T02、B14、B15、X04、X11（T01、T02、B14、B15 的行號取自步驟 3 開工前的 `c5856e54`）。
    - 新 migration `backend/alembic/versions/dc9d9290c2c0_drop_basis_attribution_columns.py`（`down_revision` 是 `545b2877e9bd`，2026-11-14 開工時重查仍是 head）：先以名稱刪掉 `ck_accepted_basis_symbol_amount` 與 `ck_accepted_basis_credit_attribution`，再刪 `accepted_capital_basis_symbol.unattributed_credits` 與 `accepted_capital_basis_credit.attribution_basis`，最後以原名重建 `ck_accepted_basis_symbol_amount`（`available >= 0 AND offered >= 0 AND credits >= 0 AND foreign_offers >= 0`）。`DROP COLUMN` 與不帶 `NOT VALID` 的 `ADD CONSTRAINT` 各帶 `squawk-ignore`（CI 的 `Migration lint (squawk)` job 會標記這兩種語句；豁免的理由與 `545b2877e9bd` 相同：部署時全停 writer）。
    - migration 自己斷言終態：刪欄前後兩張表的列數相同；兩欄在 `information_schema.columns` 不存在；`ck_accepted_basis_symbol_amount` 存在、`ck_accepted_basis_credit_attribution` 不存在；archive schema 以外沒有任何函式本體提到這兩欄（`pg_proc.prosrc`，欄位名以字界比對，與 `f486e11435c9` 同一種寫法）；`bfx_webapi` 對兩張表可讀的欄位等於刪欄前的集合減去被刪的欄（正式環境就是 symbol 表剩 `basis_id`、`symbol`、`available`、`offered`、`credits`）。授權的斷言寫成前後比對、不寫死五欄，因為角色是整個 cluster 共用的：先 migrate 到上一版、之後才建角色的資料庫，寫死的清單會讓 migration 無故拒絕。列數的斷言照全域約束「migration 自己斷言資料」保留（開工時的設計審查認為刪欄不會改列數、這條不可能失敗；完成前的 diff 審查認為拿掉是放寬契約；兩者相抵，照原計畫）；列與其餘欄位的值不變另由 migration 測試證明。`downgrade` 照 forward-only 的 ADR 直接 raise。`ledger_contract = "preserved"`：這支 migration 只動兩張 basis 表，不碰 projector cursor 與 projection archive（`core/schema_head.py` 的定義）。
    - `ledger/tables.py` 拿掉兩個欄位與 `ck_accepted_basis_credit_attribution`，`ck_accepted_basis_symbol_amount` 改成新的式子；`basis.py` 拿掉步驟 2 的兩個過渡寫入。
    - `backend/ARCHITECTURE.md`：步驟 4 寫的「兩欄仍在、寫過渡值」改成「已刪除（D1）」，放在同一個 PR（合併前補上）。合併前先把 `dev` 併進分支；第一次發版之後若有別的 migration 進了 `dev`，把 `down_revision` 改接當時的 head。
    - 測試（inventory X04、X11）：head 上的原生 SQL 拿掉這兩欄（`test_ledger_schema_roles.py` 的 `bfx_bot` 寫入測試、`test_ledger_restore_verification.py` 的 v2 basis seed、`test_ledger_webapi_grants.py` 的欄位清單）。`test_ledger_schema_roles.py` 的 `_seed` 與 `test_ledger_conservation_migration.py` 的 `_insert` 同時跑在 head 與較早的 revision，依資料庫有沒有 `unattributed_credits` 欄決定寫不寫（與既有的 `_has_realm_column` 同一種寫法）。`test_webapi_privilege_allowlist.py` 只改 head 的 `EXPECTED_COLUMNS`；`REVERSIBLE_COLUMNS` 與 `PREVIOUS_COLUMNS` 是歷史 revision 的清單，不動（該檔第 178–179 行與 forward-only ADR 的 Amendment）。`test_basis_credit_constraints_and_foreign_key` 原本測 attribution CHECK 的案例換成 `ck_accepted_basis_credit_kind`。新增 migration 測試檔 `test_drop_basis_attribution_columns.py`：以正式環境的授權建到上一個 revision、放一份有值的 basis，升級後列與其餘欄位的值不變、兩欄不存在、CHECK 仍擋負數、webapi 可讀的欄位如上、downgrade 被拒絕。
  - 消費端：`git grep -nE 'unattributed_credits|attribution_basis' -- backend/src backend/scripts backend/tests deploy`（既有 migration 檔裡的欄位清單是歷史，不改；`modules/live_validation/` 的同名計數不相干）。
  - 不能動：既有 migration 檔；`accepted_capital_basis_credit_cell` 表；其他欄位與 CHECK；`ledger_digest.py`；任何既有列的內容。
  - 驗收：
    - `tests/test_schema_head.py` 通過；`alembic check` 無 drift 由 migration 測試在升到 head 的庫上跑（`alembic(url, "check")`），另有既有的 `test_alembic_check_is_clean_at_head`。
    - 消費端的搜尋在 `backend/src` 只剩 `modules/live_validation/`；在 `backend/alembic/versions` 只多出新 migration。
    - 單元全過；本機跑新的 migration 測試、`test_ledger_schema_roles.py`、`test_ledger_webapi_grants.py`、`test_webapi_privilege_allowlist.py`、`test_ledger_conservation_migration.py`、`test_ledger_basis.py`、`test_ledger_capital_reader.py`、`test_ledger_restore_verification.py`。
    - mutation（在 repo 的複本裡做，輸出貼進進度表）：重建的 CHECK 少一個條件 → 「CHECK 仍擋負數」失敗；完全不重建 → 同一條在 setup 就失敗（migration 自己的終態斷言先擋下）；少刪一欄 → 「列不變、兩欄不存在」失敗；拿掉函式本體的斷言 → 「讀被刪欄位的函式讓升級被拒」失敗。
    - `quality_ratchet.py` 通過；CI 的 `Migration lint (squawk)` 通過。
  - 停止條件：刪欄被 PostgreSQL 以「other objects depend on it」拒絕（有 inventory 沒列到的依賴：view、index、generated column）；還原演練的測試在 seed 的 SQL 改完之後、不改 `ledger_digest.py` 的情況下仍失敗（P6(d) 不成立）。PL/pgSQL 的函式本體不在 PostgreSQL 的依賴追蹤內，刪欄不會被擋；這由 migration 的終態斷言擋下（有函式本體提到被刪的欄就拒絕），2026-11-14 的搜尋 `rg -n 'unattributed_credits|attribution_basis' backend/alembic/versions` 只命中欄位清單、欄位定義與 CHECK 字串。repo 外的讀者：告警定義所在的 repo（見本機個人脈絡檔）與私下保存的研究資料，2026-11-14 搜尋這兩個欄位名都是 0 個檔。
  - 完成後的狀態：PR 合併前，程式與測試留在分支 `feat/drop-attribution-columns`（PR #407）；合併後 `dev` 帶著未發版的 migration，之後進 `dev` 的其他 migration 要接在它後面，並緊接著做步驟 5b。
- [ ] **4. 架構文件改寫**（被擋於：步驟 2）
  - 範圍：inventory D01、D03，加上 D7 的結論（資金預算段寫明各 cell 曝險同值時依 cell id 排序分配）。`backend/ARCHITECTURE.md` 第 267 行（資金預算段的「歸屬該 cell 的 active credits」）、第 281 行（acceptance 存進 basis 的「credit 的 cell 歸屬」）、第 340 行（「cell exposure 只算可歸屬的部分」）、第 501 行（basis 表清單）、第 872 行（I-CAP 的 `E_cell` 定義與歸屬規則）、第 874 行（I-CC）改成 ADR Decision 1–3 的定義；寫明 `accepted_capital_basis_credit_cell` 是留著的歷史表、新 basis 不寫入，以及 `unattributed_credits`／`attribution_basis` 兩欄仍在、新 basis 寫過渡值（等於 `credits`、固定 `'unattributed'`）、沒有讀者、下一次發版刪除（D1、D8；刪除後的文字由步驟 3 的 PR 改）。純文件，不加測試。
  - 消費端：`rg -n 'E_cell|I-CAP|I-CC|credit_cell|unattributed|trades_range_uncovered|recent fill|歸屬' backend/ARCHITECTURE.md docs/runbooks docs/slo.md`。
  - 不能動：ADR 的 Context／Decision／Rationale；其他不相干的不變量段落。
  - 驗收：把上面的搜尋在 `backend/ARCHITECTURE.md` 的每一個命中行列在 PR 說明裡，逐行寫明「已改」或「可留與原因」（例如講週報歸屬或外來 offer 的行）；沒有任何一行還把「歸屬到 cell 的 active credits」「trade／carry／recent fill」「`trades_range_uncovered`」寫成現行行為；文件裡提到的每個符號與路徑都以 `rg` 在程式裡找得到。
- [ ] **5. 第一次發版（步驟 1、2、4）與正式環境驗證**（被擋於：步驟 1、2、4；owner 核准發版）
  - 範圍：開 `dev`→`main` 的發版 PR（merge commit），CI 與 Security 綠之後由 owner 核准合併；`bfx-deploy` 以 digest 部署。這次發版不帶 migration：開發版 PR 前確認 `git diff origin/main origin/dev -- backend/alembic/versions` 沒有輸出，有的話停下回報。其餘照 `docs/runbooks/deploy.md`。
  - 預期的行為變化（寫進發版 PR 的說明）：部署後第一份 basis 起，每個 cell 的 `E_cell` 少掉原本歸屬給它的 credit，headroom 變大；bot 會在 `spendable` 與 `cell_limit` 允許的範圍內補掛單。總額、保留、單筆上限、天期、利率下限、掛單數這些包絡不變。
  - 驗收（正式環境，唯讀查詢）：
    - 部署成功：`readlink /home/ubuntu/bfx-releases/current` 指向新 release，`journalctl -u bfx-deploy` 該次無錯誤。
    - 部署後的最新 basis：`schema_version = 2`；`accepted_capital_basis_credit_cell` 沒有該 basis 的列；每個 symbol 的 `accepted_capital_basis_cell` 金額加總等於 `accepted_capital_basis_symbol.offered`；symbol 列的 `unattributed_credits` 等於 `credits`、`block` 不含 `trades_range_uncovered`，`conservation` 不是 `unexplained_lending`。
    - 部署後 30 分鐘的 bot log 沒有錯誤：`docker logs --since <部署完成時間> bfx-bot 2>&1 | grep -ciE '"level": "(error|critical)"|Traceback'` 為 0。執行前先用 `docker logs --tail 5 bfx-bot` 確認 log 仍是這個 JSON 格式；格式不同就改用對應的寫法並記在進度表。
    - API 不再有「未歸屬」欄位：由發版 commit 的 CI 契約檢查證明（image 由同一個 commit 建），不另外在正式環境打 API。
    - 還原演練通過（新舊 basis 並存時逐列雜湊相等；開機檢查對每個 symbol 有讀取）：排程是每月 1 日 09:17 UTC（`docs/runbooks/operations.md` §8 的 `bfx-restore-test.timer`），不等它；部署後、至少有一份 `schema_version = 2` 的 basis 進了備份之後，在 VM 手動啟動一次 `sudo systemctl start bfx-restore-test@current`，以 `journalctl -u 'bfx-restore-test@current'` 與演練的 receipt 讀結果，並確認 receipt 的 bounds（`observation_query_revision`）不小於第一份 v2 basis 的 observation 的 query revision；不滿足就等下一次備份再跑。沒跑之前回報為待驗。
    - 觀察期（預設，可推翻）：上面各條通過之後再觀察 3 天，涵蓋一筆 2 天期的 credit 還款並重新掛出；期間每天查一次最新 basis 的 `block` 與 `conservation`，沒有出現下面的回滾觸發條件，才算通過，步驟 3 的 PR 才能合併。
  - 回滾：這次發版沒有 migration，上一版 image 能在同一個 schema 上開機。新版不健康時 `bfx-deploy` 自動退回上一個成功的 digest（`deploy.md` 第 5 節的 `rolled_back`）。新版健康但行為不對（觸發條件：最新 basis 出現新的 fact block，或 `conservation` 轉為 `unexplained_lending`）：先以 `sudo bfx-op halt` 停掉放貸並撤單（緊急控制，不是正常流程），再由 owner 決定部署上一版 digest 或發修正 release。上一版遇到 v2 basis 的行為是讀程式的推論、未實測（D8）：舊的 credit 不歸屬到任何 cell、不會被 `trades_range_uncovered` 擋；退回後第一份 basis 若出現新的 block，照同一個觸發條件處理。
  - 停止條件：上面任何一條驗收不成立。先停放貸並回報，再決定退回或修正。
  - 需要人做的事：owner 核准並合併發版 PR；VM 的唯讀查詢與手動啟動還原演練需要 owner 通過 Tailscale SSH 的瀏覽器驗證。
- [ ] **5b. 第二次發版（步驟 3 的刪欄）與正式環境驗證**（被擋於：步驟 5 的驗收與觀察期；步驟 3 的 PR 合併；owner 核准發版）
  - 範圍：步驟 3 的 PR 合併進 `dev` 後，開 `dev`→`main` 的發版 PR（merge commit）。這次發版帶 migration，所以 `bfx-deploy` 會先停 webapi 與 bot、備份、跑還原測試（在還原副本上演練這個 migration 並跑候選 image 的開機檢查），通過才對正式庫 migrate；還原測試或演練失敗時正式庫還沒動，部署不會開始。停機時間由演練量出，部署前回報。行為不變（寫進發版 PR 的說明）。
  - 驗收（正式環境，唯讀查詢）：
    - 部署成功：同步驟 5 的第一條。
    - schema：`alembic_version` 是 `dc9d9290c2c0`（或步驟 3 合併時改接後的 revision）；`information_schema.columns` 裡 `accepted_capital_basis_symbol.unattributed_credits` 與 `accepted_capital_basis_credit.attribution_basis` 不存在；兩張表的列數不少於部署前（部署前先記下）。
    - 部署後的最新 basis 與 30 分鐘的 bot log：同步驟 5 的第二、三條（不含 `unattributed_credits` 那一項）。
    - 還原演練：部署後、至少有一份新 schema 的 basis 進了備份之後，照步驟 5 的做法手動跑一次。目標標籤早於這次 migration 的演練會先被開機檢查以 `boot_schema_head_mismatch` 拒絕，任何帶 migration 的發版都一樣。沒跑之前回報為待驗。
  - 回滾：不能部署上一版 digest（上一版 image 在新 schema head 上拒絕開機），也不 downgrade。migration 之前失敗（還原測試、演練）：正式庫沒動，舊的 bot 與 webapi 由 `bfx-deploy` 重新啟動，修好再發。migration 之後出問題，往前修（新 commit → CI → GHCR → `bfx-deploy`；耗時沒有量過，部署前以第一次發版的 CI 與 release 實際耗時估計並回報），依 bot 的狀態分兩種：
    - bot 在跑（觸發條件：部署後最新 basis 出現新的 fact block，或 `conservation` 轉為 `unexplained_lending`）：先以 `sudo bfx-op halt` 停掉放貸並撤單（緊急控制，不是正常流程），再發修正 release。
    - bot 起不來（觸發條件：bot 開機失敗；此時 `bfx-deploy` 已停掉 bot 與 webapi）：`bfx-op` 與營運 UI 都不可用（`docs/runbooks/deploy.md` 第 5 節末）。停機前已掛在 venue 上的 offer 留在原處，金額與利率仍在發版前的包絡內，但沒有人管；要不要在 Bitfinex 網頁手動撤單由 owner 當場決定（`deploy.md` 的既有緊急程序），修正 release 部署後 bot 自己接手。
  - 停止條件：上面任何一條驗收不成立。
  - 需要人做的事：同步驟 5。
- [ ] **6. 收尾**（被擋於：步驟 5b）
  - 完成定義逐項核對（上面的清單，逐步驟）。
  - 獨立設計審查：對 `dev` 上步驟 1–4（含步驟 3）的累積 diff 跑 design-review，檢查有沒有沒重新評估就沿用的機制、繞著既有結構的補丁、留成 pending 的項目。
  - ADR 的 Result 段換成實際結果（inventory D02），包含：`schema_version` 升版本身不需要 migration（P9）、兩個欄位以 migration 刪除與 v1 digest 不能重算（D1、P14）、D2–D4 的結論、`venue_observation.py` 的 trades 起點（P13）。在 `docs/adr/2026-10-02-ledger-s1-port-and-wire-contracts.md` 第 83 行的 positions wire 契約標註 `unattributedLent` 由本 ADR 取代（inventory D04）。
  - `check_inventory.py` 是只在本階段有意義的一次性檢查，留在附件，不接進 CI；擋住回歸的是 CI integration 裡的測試（required check），gate mutant M19 證明那條測試仍擋得住（`capital-rules.yml` 在 PR 上由路徑觸發、另有每晚排程，不是 required check）。
  - 延後項（上面「延後」一條）附觸發條件搬回主清單。
  - 需要人做的事：分支 `fix/basis-cell-attribution` 與它的 worktree 是否刪除（刪遠端分支要 owner 同意）；本機個人脈絡檔「進行中的計畫」那一行更新。
  - 驗收：`python3 docs/plans/2026-11-14-cell-limit-resting-offers/check_inventory.py`；設計審查的報告與處理結果寫在進度表；ADR 的 Result 段不再有「尚未實作」。

## 決定

- **D1 `unattributed_credits` 與 `attribution_basis` 兩個欄位的去向**（狀態：已決 2026-11-14，Will；擋住步驟：2、3）
  - A：留欄，新列寫固定值——不帶 migration，可以用上一版 digest 回滾；留下兩個從此不變的欄位。
  - B：兩欄改成可為 NULL，新列寫 NULL——要 migration，回滾只能往前修。
  - C：刪掉兩欄——要 migration，回滾只能往前修；舊 basis 的歸屬紀錄（這兩欄的值）消失，v1 的 digest 不能再重算（P14）。
  - 結論：C。步驟 2 先寫過渡值，步驟 3 以 migration 刪欄。「同一次發版」由 D8 改為晚一版。ADR：收尾時寫進 `docs/adr/2026-11-14-cell-limit-bounds-resting-offers-not-lent-stock.md` 的 Result。
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
  - 實作時的收斂（2026-11-14，步驟 2 的 `/code-review`）：探測只對有 capital policy head 的 symbol 做。沒有 cell 列也沒有 policy 的錢包幣別（例如零頭）不讀：reader 先回 policy 的 block 才看 query，還原點落在觀測週期中時，這種幣別會讓健康的副本被判成 `boot_capital_read_failed`。有測試與 mutation（進度表）。

- **D5 零成交的 terminal history 列要不要納入 provenance 矛盾檢查**（狀態：已決 2026-11-14，Will；擋住步驟：無）
  - 背景：步驟 2 的設計審查與 `/code-review` 都指出，D3 的理由是「`_reflected` 會用 terminal 列把 attempt 判成已反映」，而 `_reflected` 不看成交量；被取消、零成交的 terminal 列同樣能讓兩個 attempt 都被判成已反映，卻不在 D3 列的對象裡。只在「offer 在兩份 basis 之間掛出又取消、而且有兩個 attempt 都宣稱它」時才有差別（讀程式，未實測）。
  - A：對象改成每一筆 terminal history 列——與 D3 的理由一致；比 D3 再多擋一類。（建議）
  - B：維持只查有成交的列，在程式註解寫明零成交的列不查與原因。
  - 結論：A。`write_basis` 對每一筆 terminal history 列都查，不看成交量。
- **D6 `credit_provenance_conflict` 要不要列為保護級的 block**（狀態：未決——Will 2026-11-14 選 A，實作後的 `/code-review` 查出下面「實作後才知道的事實」，A 達不到它的目的，退回重新拍板；擋住步驟：無）
  - 背景：同一類矛盾在 offer 還掛著時是 `offer_provenance_conflict`，列在 `execution/safety/protection.py` 的 `CAPITAL_BLOCK_TRIGGERS`（`IDENTITY_CONFLICT`：重試解不了，停下）。`credit_provenance_conflict` 不在表裡，改動前後都一樣：它只擋住該幣別的支出，terminal 列或 trade 滑出查詢範圍後 block 自己消失，兩個 attempt 仍被記成已反映（最後一句是兩位審查者讀程式的推論，未實測）。D3 把對象擴大到每一筆 trade，等於擴大這種會自己消失的暫停。
  - A：加進 `IDENTITY_CONFLICT`，與掛著的 offer 同一種處置——行為變更，要補測試與 runbook，並確認停下之後的自動恢復路徑。（建議；恢復路徑尚未查）
  - B：維持暫時性，在 D3 與程式註解寫明是刻意的。
  - 實作後才知道的事實（讀程式的推論，未實測；A 的程式在本機分支 `wip/d6-identity-conflict-halt`，沒有 push）：
    - 矛盾只看得到大約一個 history margin（60 秒）：看到 terminal 列的那一份 basis 把兩個 attempt 都記成已反映，它們就離開下一份 basis 的考慮範圍，查詢範圍往前移，那一列不再出現。停機 15 分鐘後自動恢復時，「兩個 attempt 宣稱同一個 offer」仍然留在帳上。
    - 代價：一次停機會撤掉所有受管 offer，並用掉 24 小時 2 次自動恢復的其中一次；一天兩次這種矛盾就要 operator 用 CLI 恢復，違反「放貸全自動」。
    - 會不會觸發看時機：capital read 只用排序後的第一個 reason 判定 trigger，同一個幣別剛好有送單中的 attempt（`attempt_outcome_missing` 排在前面）就不觸發；沒有設定 cell 的幣別也不會被讀到。
    - offer 還掛著時已經因 `offer_provenance_conflict` 停機的，停機撤單產生的 terminal 列會再觸發一次。
  - C：不讓矛盾的 attempt 被記成已反映（留在 unresolved），讓它走既有的 `unclassifiable_commitment`：那個 trigger 在條件還在時每一輪都會再觸發，會一直停到矛盾被處理。這才擋得住 D3 擔心的「兩個都被判成已反映」；要改 `_reflected` 的呼叫處，並設計矛盾怎麼解除（不能靠人工步驟當正常流程），需要另一份設計。
  - 目前的行為是 B（暫時擋住該幣別、之後自己消失）。建議：不做 A；要不要做 C 取決於這種矛盾實際會不會發生——正式環境的 journal 有沒有任何一個 venue offer 被兩個 attempt 宣稱，一條唯讀查詢就能回答，查完再決定。
- **D7 同幣別的 cell 都沒有掛單時，分配固定先給名稱排序在前的 cell**（狀態：已決 2026-11-14，Will；擋住步驟：無）
  - 背景：`allocate_capital` 依 `cell_exposure` 由小到大分配，同值時依名稱。credit 不再計入 cell 之後，錢全借出去再還款時每個 cell 的 `cell_exposure` 都是 0，名稱在前的 cell 每次先拿到 `min(spendable, cell_limit)`（最多 70%），其餘才給下一個。以前借出的存量會把較重的 cell 排到後面。ADR 的 Rationale 接受「資金全照其中一個訊號的利率借出」，但沒有寫到這個固定偏向。
  - A：接受，補一條 sizing 測試釘住同值時的順序，寫進步驟 4 的架構文件。（建議）
  - B：同值時改用別的規則（例如輪流，或依上一次成交時間），需要另外的設計。
  - 結論：A，接受。同值時的順序已有測試釘住（`tests/modules/execution/deployment/test_sizing.py` 的 `test_equal_exposure_breaks_ties_by_cell_id`；審查說沒有測試是錯的），不必再補；步驟 4 的架構文件寫明這個順序，ADR 的 Result 在步驟 6 一併記下。

- **D8 刪欄的 migration 跟步驟 1、2、4 同一次發版，還是晚一版**（狀態：已決 2026-11-14，Will；擋住步驟：3 的 PR 合併、5、5b）
  - 背景：D1 選 C 時列的做法只有「同一次發版」。同一次發版帶 migration，所以步驟 2 的行為變更（cell 上限的算法）出問題時不能自動退回上一版 image，只能往前修；步驟 5 列的三個回滾觸發條件都是行為變更的風險，不是刪欄的。全域約束寫的理由（過渡值不進正式環境）不成立為必要條件：過渡值滿足既有 CHECK，步驟 2 的程式現在就跑在舊 schema 上。
  - A：維持同一次發版——少一次帶停機的部署；行為變更出問題只能往前修（D1 選 C 時已接受「回滾只能往前修」）。
  - B：分兩次。先發步驟 1、2、4（沒有 migration，新版不健康時 `bfx-deploy` 自動退回上一版 digest），在正式環境確認穩定後再發步驟 3——業界對刪欄的一般做法（先停用、下一版才刪）；代價是多一次帶 migration 的部署，步驟 3 的 PR 要等第一次發版後才合併，步驟 4 的文件要分兩次寫（第一次寫「欄位仍在、寫過渡值」）。
  - B 成立的前提：上一版 image 退回後讀到 v2 basis（沒有 credit_cell 列、`unattributed_credits = credits`）仍能運作。2026-11-14 讀 `origin/main` 的程式，推論可以（未實測）：`capital.py` 只要求 `unattributed_credits <= credits`；`basis.py` 的 carry 規則以上一份 basis 的 credit 列判斷「已帶過」，不看有沒有 credit_cell 列，所以舊的 credit 不會觸發 `trades_range_uncovered`，只是不歸屬到任何 cell。
  - 結論：B。全域約束、步驟 3 的合併條件、步驟 4、步驟 5 已照此改寫，新增步驟 5b。

拍板順序：D1–D4 在 2026-11-14 由 Will 一次拍板（D1：C；D2：A；D3：A；D4：A）。D5–D7 是步驟 2 的審查冒出來的，2026-11-14 由 Will 一次拍板（都選 A）；D5 的程式在步驟 2 之後的小 PR，D7 只改文件（步驟 4）；D6 退回未決（見該題）。D8 是步驟 3 開工時的設計審查冒出來的，2026-11-14 由 Will 拍板（B）。

## 進度

狀態值：未開始／進行中／程式完成待驗收／完成。

| 步驟 | 狀態 | 已跑的驗收 | 未跑的驗收與原因 | commit |
|---|---|---|---|---|
| 1 讀取鏈、API 與營運 UI 拿掉「未歸屬」 | 完成 | 2026-11-14：消費端搜尋只剩 `basis.py`、`tables.py`、`modules/live_validation/`，文案鍵 0 行；本機 `mypy`、`ruff`、`lint-imports --no-cache`（26 kept）、三個受影響的 integration 檔（53 passed）、`pnpm lint`、`pnpm test`（167 passed）、`quality_ratchet.py`；本機單元 5383 passed、1 failed（`test_tick_candles.py::test_a_venue_that_never_answers_does_not_stall_the_tick`，單獨跑通過，CI 的 unit job 通過）；PR #400 的 CI 與 Security 全綠（unit、integration、Frontend、e2e smoke、契約檢查、Quality ratchet、Capital mutation gate）；`test_ledger_positions_offers.py` 檔頭的 mutation（`lent = credits + offered`）在複本實跑，被 `test_positions_use_only_the_latest_basis` 擋下；`/code-review`：findings 7（blocking 0），fixed 3、recorded 2、rejected 2、raised 0；`tests_executed` 5862→5860（兩條 `Removed-Test:`） | 無 | `e87b106d`（PR #400） |
| 2 basis 不再把 credit 歸屬到 cell | 完成 | 2026-11-14，分支 `feat/basis-resting-offers-only`：`check_inventory.py`（`67 rows, 41 files named by the pattern, 0 errors`）；驗收的 `rg` 在 `basis.py` 0 行；本機 `ruff`、`mypy`（326 檔）、`lint-imports --no-cache`（26 kept）、`quality_ratchet.py`；單元全過；受影響的 27 個 integration 檔（非 e2e）全過，含 `test_ledger_conservation.py` 整檔（P4 成立）；`capital_mutation_gate --include-integration`：`All 19 mutants killed`（M19 被 `test_lent_credit_counts_in_the_symbol_and_in_no_cell` 擋下）。複本裡的 mutation（`red-proof`，每條 base pass、canary fail、mutant killed）：credit 加回 cell → `test_lent_credit_counts_in_the_symbol_and_in_no_cell`；加回 `trades_range_uncovered` → `test_a_credit_opening_outside_the_trades_range_does_not_block[40001-100000-credit]`；拿掉 D3 的檢查 → `test_an_ended_offer_with_contradictory_provenance_blocks_its_symbol[history]`，只拿掉對 trade 的檢查 → 同一條的 `[trade]`；開機檢查拿掉探測 → `test_boot_check_reads_a_symbol_whose_capital_is_all_lent`；探測不看 policy → `test_boot_check_does_not_read_a_wallet_symbol_that_cannot_lend`；trades 起點推回 opening → `test_trades_share_the_history_range_whatever_the_credit_openings`。修改前的版本（`de0f420a` 疊上新測試）：`test_a_fully_lent_offer_leaves_its_cell_empty_and_writes_no_credit_cell` 失敗。v1 之後接 v2 的逐列雜湊：`test_a_basis_without_credit_cells_after_one_with_them_matches`。`tests_executed` 5860→5858（刪 9 條、增 7 條，`Removed-Test:` 在 commit）。審查：開工時設計審查 findings 6（blocking 0），fixed 1、recorded 3、raised 2（D5、D6）、rejected 0；完成前設計審查 findings 4（blocking 0），fixed 1、recorded 2、raised 1（D6，同上）、rejected 0；diff 視角 findings 4（blocking 0），fixed 4、recorded 0、raised 0、rejected 0；`/code-review` findings 7（blocking 0），fixed 2、recorded 1、raised 3（D5、D6、D7）、rejected 1（改 reason 字串：D3 明定不變） PR #403 的 CI 與 Security 全綠（unit、integration、Frontend、e2e smoke、Quality ratchet、Capital mutation gate、PR size）。D5 的後續（另一個 PR）：`test_an_ended_offer_with_contradictory_provenance_blocks_its_symbol[canceled]`；複本裡的 mutation：加回成交量的篩選 → 那一條失敗 | `alembic check`：本機未跑（worktree 沒有資料庫設定；這一步沒有改 `tables.py` 與 `alembic/`） | `1da218f3`、`708b7add`、`c2b6de32`、`b6770c5d`（PR #403，merge `600f955f`） |
| 3 刪掉 `unattributed_credits` 與 `attribution_basis` 兩欄 | 程式完成待驗收（PR #407 開著；D8：步驟 5 的驗收與觀察期通過後才合併） | 2026-11-14，分支 `feat/drop-attribution-columns`：alembic head 重查仍是 `545b2877e9bd`；`check_inventory.py`（`67 rows, 42 files named by the pattern, 0 errors`）；消費端搜尋在 `backend/src`、`backend/scripts`、`deploy` 只剩 `modules/live_validation/`；本機 `ruff`、`mypy`（326 檔）、`lint-imports --no-cache`（26 kept）、`quality_ratchet.py`；單元 5396 passed；受影響的 9 個檔（新的 migration 測試、`test_ledger_schema_roles.py`、`test_ledger_webapi_grants.py`、`test_webapi_privilege_allowlist.py`、`test_ledger_conservation_migration.py`、`test_ledger_basis.py`、`test_ledger_capital_reader.py`、`test_ledger_restore_verification.py`、`tests/test_schema_head.py`）163 passed，其中 migration 測試升到 head 後 `alembic check` 無 drift。複本裡的 mutation（每條 base pass、canary fail、mutant killed）：重建的 CHECK 少掉 `foreign_offers >= 0` → `test_the_amount_check_still_refuses_a_negative_amount[foreign_offers]`；完全不重建 → 同一條在 setup 失敗（migration 的終態斷言）；少刪一欄 → `test_the_upgrade_keeps_the_rows_and_drops_both_columns`；拿掉函式本體的斷言 → `test_a_function_that_reads_a_dropped_column_refuses_the_upgrade`。修改前的版本（`origin/dev` 疊上新測試檔）：紅（找不到 revision `dc9d9290c2c0`）。`tests_executed` 5859→5867（新增 8 條）。審查：開工時設計審查 findings 8（blocking 0），fixed 6、recorded 1、raised 1（D8）、rejected 0；diff 視角 findings 4（blocking 0），fixed 4、recorded 0、raised 0、rejected 0；完成前設計審查 findings 6（blocking 0），fixed 4、recorded 1、raised 1（D8，同上）、rejected 0；`/code-review` findings 8（blocking 0），fixed 6、recorded 1、raised 0、rejected 1（測試檔重用別檔的授權查詢 helper：那個 helper 逐一掃所有表，這裡只需要兩張表）。PR #407 的 CI 與 Security 全綠（2026-11-14，head `1a789a2d`：unit、integration、Frontend、e2e smoke、Quality ratchet、Capital mutation gate、`Migration lint (squawk)`、gitleaks、PR size） | 無（合併前把 `dev` 併進分支後要重跑 CI） | `0b2ea90f`、`5230b479`、`1a789a2d`（PR #407，未合併） |
| 4 架構文件改寫 | 未開始 | | | |
| 5 第一次發版（步驟 1、2、4）與正式環境驗證 | 未開始 | | | |
| 5b 第二次發版（步驟 3 的刪欄）與正式環境驗證 | 未開始 | | | |
| 6 收尾 | 未開始 | | | |
