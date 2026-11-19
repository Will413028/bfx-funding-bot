# 策略研究改以市場成交紀錄為地基

**Goal：** 每個候選策略都用 Bitfinex 公開 funding 成交紀錄逐筆 replay 來評估：每個決策變成一張假想掛單，對之後的實際成交判定成交量與成交時間，收益依實際成交與閒置時間計算，報告附保守與樂觀邊界。期望值記帳（利率 × 成交機率）、`linear-baseline` 與 K 線成交 learner 從研究路徑移除；每週研究工作自動收集成交紀錄並追蹤 replay 與自己成交的偏差。驗收見步驟 8。

**上層：** 主清單見本機個人脈絡檔（`AGENTS.local.md`）。

**附件：** `2026-11-15-trade-tape-research-method/`（`inventory.md` 盤點，快照 `a1452398`，去向 2026-11-20 改寫；`spike/` 步驟 1 的腳本與報告）。

**決定紀錄：** ADR `docs/adr/2026-11-20-research-fills-by-trade-tape-replay-tape-as-daily-files.md`（D1、D2，Will 2026-11-20）。2026-11-20 前的版本以「tape 查表成交機率＋Postgres 表」為設計，步驟 1 完成後被該 ADR 取代，舊步驟 2–7 已改寫如下（舊版見 commit `dd89f5df`）。

## 全域約束與完成定義（暫代主計畫）

- live money path 不得讀取本計畫的任何產物（成交紀錄檔、replay 報告）；本計畫只改研究路徑與每週研究工作。book learner、`FillRateModel`、`estimate_fill` 留給 `OPTIMIZER_LIVE` 未啟用路徑，不刪不改。
- 不新增依賴；需要時先列套件與版本等 owner 同意（`AGENTS.md`「供應鏈與依賴」）。
- 每個新 sweep／回測照既有預登記規則：開跑前登記 mechanism 與宣告空間，DSR trials 累計同步 `run_oos_profitability.py` 的 `DEFAULT_N_TRIALS`。
- 在 VM 上呼叫 Bitfinex 公開 API 一律 20 秒間隔、第一次 429 就停（VM 與 live bot 共用出口 IP；超限時整個 IP 封鎖 60 秒）。
- 完成定義：盤點每列去向已落地；新增行為有「破壞新行為要失敗」的測試；`uv run pytest -m "not integration"`、`mypy`、`ruff`、quality ratchet 綠；integration 由 CI 跑。

## 前提（核對日期見各條）

- 會改到的既有部署是否在線：**在線**（VM，`bfx-app` 與每週 `bfx-weekly-report`）。會碰到正式環境的是步驟 2 的成交紀錄目錄、R2 同步與每週工作的 compose 改動：回滾＝revert compose 與 unit 檔、刪除成交紀錄目錄；不動 DB schema，沒有相容性窗口需求，因為 live bot 不讀這些東西（見不變量）。
- 公開 `GET /v2/trades/f{SYM}/hist` 回傳全市場成交 `[ID, MTS, AMOUNT, RATE, PERIOD]`，fUSD／fUST 有 2022-01 起的完整資料，沒有一天無成交（步驟 1 實測，2026-11-20；已確認）。
- AMOUNT 正負號＝主動成交方：負＝借方主動吃放貸掛單，正＝放貸方主動吃借方掛單（4 筆自己成交對照，2026-11-15；已確認，樣本小）。規則在 `.claude/rules/bitfinex-venue.md`（PR #418，已合併）。
- 撮合規則：offer 期限必須 ≥ bid 期限才能撮合；固定利率單不和 FRR 單撮合；價格優先、同價先到先撮（[Bitfinex support](https://support.bitfinex.com/hc/en-us/articles/115003428165-Margin-Funding-on-Bitfinex)，2026-11-20 讀；已確認）。
- 公開紀錄的 PERIOD 記的是 bid 還是 offer 的期限：**未確認**（步驟 1b 驗）。
- 公開紀錄是否包含所有成交（例如 FRR 成交、隱藏單）：**未確認**（步驟 1b 盡力驗，驗不到就寫成已知限制交 Will）。
- 私有 funding trades 的 trade id 與公開紀錄的 ID 相同（實測 2026-11-15；已確認），可用 trade id join。
- trade id 是跨 symbol 共用的序號（步驟 1 實測；已確認）；同 symbol 依時間排序時 4.6–7.2% 的 id 往回跳，續抓游標用 MTS、同一 MTS 依 id 去重（已確認）。
- 密度（步驟 1 全量；已確認）：fUSD 1.03 億列、fUST 3,957 萬列；p30 每日中位數 fUSD 600、fUST 77，fUST 有 72 天沒有 p30 成交、最長 7.9 天。
- 頻率限制：20 秒間隔 10,590 次請求 0 次 429；完整回補約 3.5 天；一週增量 794 秒（步驟 1 實測）。
- 全量 gzip CSV 1.27 GB（fUST 351 MB、fUSD 920 MB；VM 實測 2026-11-20）。VM 磁碟剩 144G（2026-11-19）。
- 每週研究工作現有各步 timeout 合計 5100 秒，`TimeoutStartSec=5400`（`deploy/vm/systemd/bfx-weekly-report.service:20-25`；已確認 2026-11-15）。
- book 快照只從 2026-07-19 起（實測）；之前的同價成交無法估排隊位置。
- 已報告的研究數字（期限回測 2026-09-22、OOS）用 `linear-baseline` 或期望值記帳（`backtest/period_structure.py:278`、`backtest/engine.py` `_apply_friction`；已確認）。

**驗收基線：** 2026-11-15 在 `feaa9743` 跑：單元 5415 passed、`mypy src/`、`ruff check`、`quality_ratchet.py` 全綠（步驟 1 進度列）。

## 本階段依賴的不變量

| 不變量 | 依賴它的機制 |
|---|---|
| live bot 不建 `FillRateModel`、不讀成交模型表或成交紀錄檔 | `apps/config.py:140`（`BFX_FILL_MODEL_ARTIFACT` 只在 `OPTIMIZER_LIVE`）、`execution/deployment/reconciler.py:155-164,195,715-736`（`fill_model_provider` 預設 None） |
| `OPTIMIZER_LIVE` 路徑的 `estimate_fill` 簽名與 book artifact 不變 | `lending/tracking/model.py:117`、reconciler 的 `FillModelEvidenceProvider` protocol、`scripts/learn_book_fill_rate.py` 與每週工作 :84 |
| 成交紀錄的每日檔案在該日結束後不可變；同一天重抓結果相同 | 步驟 2 的收集工作（筆數與雜湊比對）、步驟 3 的 replay 讀取 |
| 成交紀錄以 venue trade id 為唯一身分 | 步驟 2 的每日檔去重、步驟 4 剔除自己成交與對照自己成交的 join（`funding_trades.trade_id`） |
| 每週研究工作各步 timeout 總和 < systemd `TimeoutStartSec` | `deploy/vm/systemd/bfx-weekly-report.service:25`、`deploy/vm/ops/docker-compose.weekly-report.yml:74-89` |
| 研究回測缺資料時 fail-closed（不補近似值） | `BacktestIncomplete`（`backtest/engine.py:48-62`）；ADR 2026-09-22 Amendment D5。機制會變：步驟 3 新增「成交紀錄缺日」原因，步驟 5 刪 `fill_model_*` 原因 |
| 研究容器的 rootfs 唯讀，可寫路徑只有允許清單 | `deploy/vm/ops/compose_policy.py --kind weekly-report`、`docker-compose.weekly-report.yml:21-24`；步驟 2 為收集 service 新增可寫掛載 |

## 範圍

- 做：成交紀錄每日檔案的回補與每週增量收集（fUSD、fUST），R2 同步依 D5；replay 引擎（相容期限、穿價成交、部分成交、同價時的排隊模型、保守與樂觀邊界、成交時間與閒置時間）；用自己成交驗證 replay；runner 全面改走 replay，刪除期望值記帳、linear 與 K 線 learner；決策空間搜尋 runner；每週工作接上。
- 不做：live money path 使用成交模型（`OPTIMIZER_LIVE`）；fUSD／fUST 以外的幣；bandit 資金分配；Parquet／DuckDB（要新增依賴，見延後）。
- 延後：
  - 成交時間的存活模型，取代 live 的 book 查表（觸發：要討論開 `OPTIMIZER_LIVE`）。
  - 改成 Parquet + DuckDB（觸發：步驟 1b 或步驟 3 量到讀檔是 replay 的主要耗時，且超過每週工作預算）。
  - 用 live 成交做校準的正式驗收門檻（觸發：恢復放貸且自己的成交 ≥ D4 的樣本下限）。
  - bandit 資金分配（觸發：本金放大到 cap 加碼 gate 通過）。

## 步驟

- [x] **1. Spike：完整成交紀錄量測**（被擋於：無）——完成 2026-11-20，結果見 `spike/report.md`；照 Will 2026-11-15 的指示改在 VM 上抓，不在筆電抓
  - 範圍：唯讀腳本，fUSD 與 fUST 都從 2022-01-01 完整分頁抓取（資料不進 repo、不寫 DB）。輸出：每日筆數、各天期分桶筆數、正負號比例、時間空窗、全域 id 序號假設的驗證、總列數與估計 DB 大小、完整抓取耗時、一週增量的抓取秒數、429 次數。
  - 驗收：`spike/report.md` 附上述每個數字與產生它的指令。
  - 停止條件：任何 symbol 有超過 7 天沒有任何成交的空窗 → 回報，D1／D2 重評。（未觸發：最長 6.95 小時。）

- [ ] **1b. Spike：replay 的前置事實**（被擋於：無；第 1、4 項的正式 DB 查詢要 Will 核准）
  - 範圍：唯讀，產出 `spike/report-1b.md`：
    1. PERIOD 語意：`funding_trades` 的 `trade_id` join VM 上的成交紀錄 CSV 取公開 PERIOD；我方 offer 當初掛的期限取自 `funding_trades.offer_id` 對到 `ledger_observation_offer_history.venue_offer_id`（文字型別，需轉換）的 `period_days`。`funding_trades.period` 是成交本身的期限，和公開 PERIOD 同一種欄位，不能拿來當 offer 期限。公開 PERIOD 恆等於我方掛的期限 → 記的是 offer 期限；有比我方短的值 → 記的是 bid 期限。找不到 offer 期限的筆數另列。
    2. 我方規模：cell 上限與典型掛單金額，對各期限每日借方主動成交量的中位數與 p10。
    3. replay 成本：用 numpy 把一個 symbol 依期限、時間排好載入，量載入秒數與記憶體；對 1,000 張假想掛單做一次 replay，量耗時，外推到一次 WFO 掃描與每週工作。
    4. 完整性：從自己的 64 筆成交，看有幾筆在公開紀錄找得到（2026-11-15 已找到 4 筆）；找不到的列出原因。
    5. FRR 成交能否辨識：公開紀錄有沒有欄位或規律（例如利率恰等於當時 FRR）能分出 FRR 成交；給出判別依據，或寫明無法辨識。
  - 檔案：`spike/report-1b.md` 與需要的唯讀腳本（commit）；資料不 commit。
  - 消費端：無（spike）。
  - 不能動：任何 repo 程式與正式 DB。
  - 驗收：報告附每個數字與產生它的指令。
  - 停止條件：(0) 自己成交有一半以上找不到當初的 offer 期限 → 回報，第 1 項改找其他來源；(1) PERIOD 語意兩種解讀都對不上 → 回報，replay 的期限相容規則重評；(2) 我方金額超過該期限每日借方成交量 p10 的 10% → 回報，replay 要加市場衝擊；(3) 自己成交在公開紀錄找不到的比例 > 5% → 回報完整性問題；(4) 外推的每週 replay 耗時超過 1,800 秒 → 回報，評估 Parquet（延後項）。
  - 需要人做的事：第 1、4 項要對正式 DB 跑一個唯讀查詢（`funding_trades` 與 offer 期限），執行前取得 Will 核准。

- [ ] **2. 成交紀錄每日檔案與收集**（被擋於：步驟 1b）
  - 檔案：新增 `backend/scripts/ingest_public_trades.py` 與其測試；修改 `deploy/vm/ops/docker-compose.weekly-report.yml`、`deploy/vm/ops/compose_policy.py`（新 service 的可寫掛載）、`deploy/vm/ops/bfx_weekly_report.py`（掛載存在檢查）、`deploy/vm/systemd/bfx-weekly-report.service`、`backend/tests/scripts/test_vm_weekly_report.py` 與 compose policy 的測試。
  - 範圍：`backend/scripts/ingest_public_trades.py`：輸出 `<root>/<symbol>/<YYYY-MM-DD>.csv.gz`（欄位 `id,mts,amount,rate,period`，amount 帶正負號），一天結束後才寫入該日檔；從目錄現況推導要補哪些日子，冪等（重抓一天，筆數與內容雜湊相同就不動，不同就報錯不覆蓋），可中斷續跑；續抓游標用 MTS、同一 MTS 依 id 去重；20 秒間隔、第一次 429 就停、通用 UA。R2 同步依 D5（未決，不擋本步其餘部分）。收集跑在每週 compose 的另一個 service（只有它以可寫掛載這個目錄），在研究 service 之前執行；研究 service 以唯讀掛載同一目錄；兩者都沿用唯讀 rootfs，`compose_policy.py --kind weekly-report` 增加這個掛載的允許規則。
  - 消費端：`rg -ln "weekly-report|compose_policy|TimeoutStartSec|reports_mount" deploy backend/tests docs/runbooks`（每週工作的結構與預算）；新名字 `rg -n "ingest_public_trades|tape_root" backend deploy`。
  - 不能動：DB schema、`bfx_research` 權限、既有 offsite 備份內容。
  - 驗收：compose policy 測試：研究 service 對成交紀錄目錄是唯讀，把它改成可寫時測試要失敗；單元測試（續抓起點從目錄推導、重跑不重寫已存在的日檔、內容不同時報錯、429 即停、同 MTS 去重、正負號原樣保存——把 `abs()` 加回去測試要失敗、未結束的當日不寫檔）；compose 各步 timeout 總和＋300 ≤ `TimeoutStartSec`；每週增量步驟 timeout＝794 秒×3≈2,400 秒，`TimeoutStartSec` 調到新合計＋300 秒（至少 7,800），unit 檔預算註解同步。
  - 需要人做的事：發版（`dev`→`main` PR 合併＝部署，需 Will 核准）；發版後在 VM 跑完整回補（約 3.5 天，可續跑）。步驟 1 留在 VM 一次性目錄的 CSV 可直接切成日檔匯入，省下回補時間——匯入後抽 3 天與 API 重抓比對雜湊。
  - 完成後的狀態：回補完成前，步驟 3 只能用部分歷史。

- [ ] **3. replay 引擎**（被擋於：步驟 1b、步驟 2）
  - 檔案：新增 `backend/src/bfx_funding_bot/modules/backtest/replay.py` 與其測試、golden fixture；修改 `backtest/engine.py`、`backtest/config.py`、`backend/ARCHITECTURE.md`。
  - 範圍：新增 `backend/src/bfx_funding_bot/modules/backtest/replay.py`：輸入策略決策序列與成交紀錄，對每張假想掛單（利率 r、期限 P、掛出時間 t0、金額 s）輸出成交量、成交時間、閒置時間。規則：只看 t0 之後、借方主動（負號）、期限與 P 相容（依步驟 1b 的 PERIOD 語意）的成交；利率 > r 的成交依累積量成交（部分成交）；利率 = r 的成交，保守邊界不計、樂觀邊界全計，2026-07-19 之後有 book 快照時另給排隊模型估計值（可重用 `lending/tracking/queue_fill.py`）；FRR 成交若可辨識則排除。引擎新增 replay 記帳路徑，與現有期望值路徑並存（`_apply_friction` 與 runner 的 `--fill-model` 在步驟 5 才刪，讓步驟 4 的驗證能在刪除前放行）；成交紀錄缺日時 `BacktestIncomplete`，`BacktestIncompleteReason` 新增對應原因。FRR 依步驟 1b 第 5 項：可辨識就排除並加 mutation，不可辨識就寫進報告的已知限制。`backend/ARCHITECTURE.md` 的回測段落同步改寫。
  - 消費端：`rg -n "run_backtest\(|_apply_friction|BacktestConfig\(" backend/src backend/scripts backend/tests`。
  - 不能動：`OPTIMIZER_LIVE` 路徑（`FillRateModel`、`estimate_fill`、book learner）；WFO／DSR 計算。
  - 驗收：golden 測試（固定小段成交紀錄＋固定掛單 → 固定成交量與時間）；mutation：期限不相容的成交也算、正號成交也算、同價成交在保守邊界也算、部分成交改成全額、拿掉缺日的 `BacktestIncomplete`，各自要讓測試失敗；全量 replay 一次的耗時記入進度表。
  - 停止條件：replay 一次 WFO 掃描的耗時讓每週工作超出預算 → 回報，評估 Parquet（延後項）。

- [ ] **4. 用自己成交驗證 replay**（被擋於：D4、步驟 3）
  - 檔案：新增 `backend/scripts/report_replay_validation.py` 與其測試、`docs/research/` 報告。
  - 範圍：`backend/scripts/report_replay_validation.py`：(a) 把自己的歷史 offer（利率、期限、掛出時間、金額）丟進 replay，比較 replay 判定的成交時間與實際成交時間；(b) 保守與樂觀邊界的寬度分佈（邊界太寬代表同價成交佔比高，排隊位置是主要不確定性）。replay 時剔除自己的成交（join trade id），避免自己對照自己。報告寫進 `docs/research/`。
  - 消費端：無（新報告）。
  - 不能動：既有報告。
  - 驗收：報告產出且每個數字附指令；單元測試覆蓋指標計算（把分母換掉要失敗；不剔除自己成交要失敗）。
  - 放行規則（步驟 5 的「通過」）：依 D4 預登記的門檻。自己成交達不到樣本下限時，報告寫明「未達樣本下限」，只看 (b) 放行，(a) 由步驟 7 每週追蹤。
  - 停止條件：(a) 達樣本下限卻不過門檻 → 不進步驟 5，回報並比對業界排隊模型。

- [ ] **5. runner 改走 replay，刪除期望值記帳、linear 與 K 線 learner**（被擋於：D3、步驟 4 通過）
  - 檔案：刪除與修改依盤點「去向」欄（每列已寫刪除或改寫）；另修改 `deploy/vm/ops/docker-compose.weekly-report.yml`、`deploy/vm/systemd/bfx-weekly-report.service`、`backend/ARCHITECTURE.md`、相關 ADR。
  - 範圍：照盤點「去向」欄逐列落地：刪 `compute_fill_prob`、`fill_alpha`、`fill_model` Literal 與 empirical／linear 分支、`EMPIRICAL_SOURCES`、`FillRateLearner`／`learn_fill_rate.py`、三支一次性 runner；其餘 runner 改走 replay，刪 `--fill-model`；`period_structure.py:278`、`band_sweep.py:301` 改印 replay 與邊界；每週工作刪 linear 那步（:86）、:85 與 `run_oos_profitability`（:87）改 replay，:84 book learner 保留（live artifact）；報告檔名 `*-period-structure-book.json` 改為 `*-period-structure-replay.json`，`diff_research_report` 對新檔名從零開始比對（舊檔留在 `docs/research/` 當歷史）；`ARCHITECTURE.md` 回測與 fill model 段落改寫；提到被刪機制的 ADR（`rg -ln "linear-baseline|fill_alpha|learn_fill_rate" docs/adr`）各加一行指向 ADR 2026-11-20；`BacktestIncompleteReason` 刪 `fill_model_*` 與 `_raise_for_unavailable`；quality ratchet 測試數下降時用 `Ratchet-Reason:`／`Removed-Test:` trailer。DB 既有 candle artifact 保留不刪（artifact 不可變）。
  - 消費端：`rg -ln "linear-baseline|fill_alpha|learn_fill_rate|FillRateLearner|run_phase3b_wfo_matrix|run_phase4_3|explore_mr_param|compute_fill_prob|--fill-model|EMPIRICAL_SOURCES" --hidden -g '!.git' .`
  - 不能動：book learner、`FillRateModel`、`estimate_fill`；live 不變量。
  - 驗收：`rg -n "linear-baseline|fill_alpha|compute_fill_prob|FillRateLearner|--fill-model|EMPIRICAL_SOURCES" backend deploy .claude` 無結果；`docs/` 只剩 ADR 與 `docs/research/` 的歷史文字；mutation：把任一 runner 改回期望值記帳，測試要失敗；全套單元測試綠；每週工作 :85、:87 的 timeout 依步驟 3 量到的 replay 耗時×3 重設，compose 各步 timeout 總和＋300 ≤ `TimeoutStartSec`，unit 檔預算註解同步。
  - 需要人做的事：發版核准。

- [ ] **6. 決策空間搜尋 runner**（被擋於：步驟 5）
  - 檔案：新增 `backend/scripts/run_decision_space_search.py` 與其測試；修改 `backend/scripts/run_oos_profitability.py`（`DEFAULT_N_TRIALS`）。不進每週工作。
  - 範圍：`backend/scripts/run_decision_space_search.py`：在 replay 上搜尋（掛價偏移 × 期限 × 改價規則），沿用 calendar WFO、DSR、預登記；第一個預登記題目是重跑期限四臂（取代 2026-09-22 linear 結果）。
  - 消費端：無（新 runner）。
  - 不能動：既有 WFO／DSR 計算。
  - 驗收：單元測試（窗切分、trials 計數寫入報告）；`DEFAULT_N_TRIALS`（`run_oos_profitability.py:53`，現值 38）加上本步預登記的臂數；跑出第一份報告進 `docs/research/`。

- [ ] **7. 每週工作接上 replay 驗證**（被擋於：步驟 4、5）
  - 檔案：修改 `deploy/vm/ops/docker-compose.weekly-report.yml`、`deploy/vm/systemd/bfx-weekly-report.service`、`backend/tests/scripts/test_vm_weekly_report.py`。
  - 範圍：每週工作在 replay 報告之後跑 `report_replay_validation`，偏差超過 D4 門檻時報告標 DRIFT（不自動改任何東西）。
  - 消費端：`deploy/vm/ops/docker-compose.weekly-report.yml`、`tests/scripts/test_vm_weekly_report.py`。
  - 驗收：weekly report 測試涵蓋新步驟順序；compose 各步 timeout 總和＋300 ≤ `TimeoutStartSec`，unit 檔預算註解同步；mutation：拿掉 DRIFT 判斷測試要失敗。
  - 需要人做的事：發版核准。

- [ ] **8. 收尾**：完成定義逐項核對；盤點每列去向無空格；獨立設計審查（diff）；延後項附觸發條件搬回主清單；D4 的決定補進 ADR 2026-11-20 或另寫 ADR；研究 registry 把 linear 與期望值記帳時代的結果全部降為「待 replay 重跑」；刪除 VM 上步驟 1 的一次性目錄。驗收：`rg` 指令（步驟 5）無結果、CI 綠、`docs/research/` 有 replay 驗證與第一份搜尋報告。

## 決定

- **D1 成交紀錄存哪裡**（狀態：已決 2026-11-20，Will）：每日 gzip CSV 檔，放 VM 並同步 R2，不進 Postgres。ADR 2026-11-20 D2。
- **D2 回測怎麼判定成交**（狀態：已決 2026-11-20，Will）：逐筆 replay，取代查表成交機率 × 利率；原 D2「排隊量從哪來」只剩同價成交的排隊模型，併入步驟 3。ADR 2026-11-20 D1。
- **D3 linear 時代的研究結論怎麼處理**（狀態：已決 2026-11-15，Will）：registry 全部標「待重跑」，程式刪 linear；DSR trials 累計保留。期望值記帳時代的結果比照處理（2026-11-20 隨 D2 擴大）。
- **D4 replay 驗證的判準**（狀態：未決；擋住步驟：4、7）
  - A：自己 offer 的實際成交時間落在 replay 保守與樂觀邊界之間的比例 ≥ 預登記門檻，且排隊模型估計值的誤差中位數 ≤ 預登記門檻。（建議；門檻數字在步驟 3 完成後、步驟 4 開跑前預登記）
  - B：只看邊界涵蓋率——簡單；代價是不檢驗排隊模型。
  - 兩案都要預登記樣本下限（建議：每個期限桶 ≥30 筆自己的成交）。

- **D5 成交紀錄要不要另存一份到 R2**（狀態：未決；擋住步驟：2 的 R2 部分；2026-11-20 第 2 輪計畫審查提出）
  - 事實：既有 offsite 工具是 pgBackRest，只備份資料庫，不能上傳一般檔案；每週容器不帶任何秘密；標準函式庫做不到 S3 簽章。成交紀錄可以從 Bitfinex 公開 API 重抓（完整回補約 3.5 天）。
  - A：不另存，VM 上的日檔就是唯一副本；VM 毀損時從 API 重抓。不新增依賴、不碰憑證。代價：重建要 3.5 天；若 Bitfinex 某天不再提供舊歷史，就再也拿不回來。（建議；延後觸發：Bitfinex 縮短歷史可查範圍，或研究需要在 VM 重建後一天內恢復。）
  - B：主機上的 systemd unit 用 VM 已有的 CLI 上傳，另發一把只限新前綴的 R2 token（Will 設定）。代價：新秘密檔、新 unit、與 DR bucket 共用帳戶。
  - C：研究容器內用 Python 上傳，要新增 S3 客戶端依賴（需 owner 同意）。
  - 選 A 的話，ADR 2026-11-20 B1 的「同步到 R2」要改寫。

拍板順序：D1、D2、D3 已拍；D5 可現在拍；D4 等步驟 3。

## 進度

狀態值：未開始／進行中／程式完成待驗收／完成。

| 步驟 | 狀態 | 已跑的驗收 | 未跑的驗收與原因 | commit |
|---|---|---|---|---|
| 1 Spike 完整量測 | 完成 | 全量抓取兩幣、`--summarize`、一週增量量測、PG18 每列大小；基線（`feaa9743`）：單元 5415 passed、mypy、ruff、quality ratchet 全綠 | 無 | PR #423 |
| 1b replay 前置事實 | 未開始 | | | |
| 2 每日檔案與收集 | 未開始 | | | |
| 3 replay 引擎 | 未開始 | | | |
| 4 replay 驗證 | 未開始 | | | |
| 5 刪期望值記帳與 linear | 未開始 | | | |
| 6 決策空間搜尋 | 未開始 | | | |
| 7 每週驗證 | 未開始 | | | |
| 8 收尾 | 未開始 | | | |
