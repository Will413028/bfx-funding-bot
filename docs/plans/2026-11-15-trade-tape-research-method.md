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

- 會改到的既有部署是否在線：**在線**（VM，`bfx-app` 與每週 `bfx-weekly-report`）。會碰到正式環境的是步驟 2 的成交紀錄目錄與每週工作的 compose 改動：回滾＝revert compose 與 unit 檔、刪除成交紀錄目錄；不動 DB schema，沒有相容性窗口需求，因為 live bot 不讀這些東西（見不變量）。
- 公開 `GET /v2/trades/f{SYM}/hist` 回傳全市場成交 `[ID, MTS, AMOUNT, RATE, PERIOD]`，fUSD／fUST 有 2022-01 起的完整資料，沒有一天無成交（步驟 1 實測，2026-11-20；已確認）。
- AMOUNT 正負號＝主動成交方：負＝借方主動吃放貸掛單，正＝放貸方主動吃借方掛單（4 筆自己成交對照，2026-11-15；已確認，樣本小）。規則在 `.claude/rules/bitfinex-venue.md`（PR #418，已合併）。
- 撮合規則：offer 期限必須 ≥ bid 期限才能撮合；固定利率單不和 FRR 單撮合；價格優先、同價先到先撮（[Bitfinex support](https://support.bitfinex.com/hc/en-us/articles/115003428165-Margin-Funding-on-Bitfinex)，2026-11-20 讀；已確認）。
- 公開紀錄的 PERIOD 記的是 bid 還是 offer 的期限：**無法用自己的成交確認**（步驟 1b：自己的成交全是 2 天期，兩種解讀算出同值）。replay 改用兩種解讀下都成立的規則：假想掛單期限 P 只算公開 PERIOD ≤ P 的成交（Will 2026-11-20 選 A；`spike/report-1b.md` 第 1 節）。
- 公開紀錄是否包含所有成交：自己的 64 筆成交全部找得到（步驟 1b 實測，2026-11-20；已確認，限 fUST 2 天期）。FRR 成交無法辨識，replay 不排除，寫成偏樂觀的已知限制（步驟 1b 第 5 節）。
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

- 做：成交紀錄每日檔案的回補與每週增量收集（fUSD、fUST），不另存 R2（D5）；replay 引擎（相容期限、穿價成交、部分成交、同價時的排隊模型、保守與樂觀邊界、成交時間與閒置時間）；用自己成交驗證 replay；runner 全面改走 replay，刪除期望值記帳、linear 與 K 線 learner；決策空間搜尋 runner；每週工作接上。
- 不做：live money path 使用成交模型（`OPTIMIZER_LIVE`）；fUSD／fUST 以外的幣；bandit 資金分配；Parquet／DuckDB（要新增依賴，見延後）。
- 延後：
  - 成交時間的存活模型，取代 live 的 book 查表（觸發：要討論開 `OPTIMIZER_LIVE`）。
  - 成交紀錄另存一份到 R2（D5 的 B／C；觸發：Bitfinex 縮短公開歷史的可查範圍，或研究需要在 VM 重建後一天內恢復）。
  - 改成 Parquet + DuckDB（觸發：步驟 1b 或步驟 3 量到讀檔是 replay 的主要耗時，且超過每週工作預算）。
  - 用 live 成交做校準的正式驗收門檻（觸發：恢復放貸且自己的成交 ≥ D4 的樣本下限）。
  - bandit 資金分配（觸發：本金放大到 cap 加碼 gate 通過）。

## 步驟

- [x] **1. Spike：完整成交紀錄量測**（被擋於：無）——完成 2026-11-20，結果見 `spike/report.md`；照 Will 2026-11-15 的指示改在 VM 上抓，不在筆電抓
  - 範圍：唯讀腳本，fUSD 與 fUST 都從 2022-01-01 完整分頁抓取（資料不進 repo、不寫 DB）。輸出：每日筆數、各天期分桶筆數、正負號比例、時間空窗、全域 id 序號假設的驗證、總列數與估計 DB 大小、完整抓取耗時、一週增量的抓取秒數、429 次數。
  - 驗收：`spike/report.md` 附上述每個數字與產生它的指令。
  - 停止條件：任何 symbol 有超過 7 天沒有任何成交的空窗 → 回報，D1／D2 重評。（未觸發：最長 6.95 小時。）

- [x] **1b. Spike：replay 的前置事實**（完成 2026-11-20，結果見 `spike/report-1b.md`）（被擋於：無；第 1、4 項的正式 DB 查詢要 Will 核准）
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

- [x] **2. 成交紀錄每日檔案與收集**（被擋於：步驟 1b）——完成 2026-11-20：發版 PR #428（部署 `ede0482b`），VM 上匯入、抽查、補抓見進度表
  - 檔案：新增 `backend/scripts/ingest_public_trades.py` 與其測試；修改 `deploy/vm/ops/docker-compose.weekly-report.yml`、`deploy/vm/ops/compose_policy.py`（新 service 的可寫掛載）、`deploy/vm/ops/bfx_weekly_report.py`（掛載存在檢查）、`deploy/vm/systemd/bfx-weekly-report.service`、`backend/tests/scripts/test_vm_weekly_report.py` 與 compose policy 的測試。
  - 範圍：`backend/scripts/ingest_public_trades.py`：輸出 `<root>/<symbol>/<YYYY-MM-DD>.csv.gz`（欄位 `id,mts,amount,rate,period`，amount 帶正負號），一天結束後才寫入該日檔；從目錄現況推導要補哪些日子，冪等（重抓一天，筆數與內容雜湊相同就不動，不同就報錯不覆蓋），可中斷續跑；續抓游標用 MTS、同一 MTS 依 id 去重；20 秒間隔、第一次 429 就停、通用 UA。不另存 R2（D5＝A）：VM 上的日檔是唯一副本，VM 毀損時從 API 重抓。收集跑在每週 compose 的另一個 service（只有它以可寫掛載這個目錄），在研究 service 之前執行；研究 service 以唯讀掛載同一目錄；兩者都沿用唯讀 rootfs，`compose_policy.py --kind weekly-report` 增加這個掛載的允許規則。
  - 消費端：`rg -ln "weekly-report|compose_policy|TimeoutStartSec|reports_mount" deploy backend/tests docs/runbooks`（每週工作的結構與預算）；新名字 `rg -n "ingest_public_trades|tape_root" backend deploy`。
  - 不能動：DB schema、`bfx_research` 權限、既有 offsite 備份內容。
  - 驗收：compose policy 測試：研究 service 對成交紀錄目錄是唯讀，把它改成可寫時測試要失敗；單元測試（續抓起點從目錄推導、重跑不重寫已存在的日檔、內容不同時報錯、429 即停、同 MTS 去重、正負號原樣保存——把 `abs()` 加回去測試要失敗、未結束的當日不寫檔）；compose 各步 timeout 總和＋300 ≤ `TimeoutStartSec`；每週增量步驟 timeout＝794 秒×3≈2,400 秒，`TimeoutStartSec` 調到新合計＋300 秒（至少 7,800），unit 檔預算註解同步。
  - 需要人做的事：發版（`dev`→`main` PR 合併＝部署，需 Will 核准）；發版後在 VM 用 `--import-csv` 把步驟 1 留下的 CSV 切成日檔（來源的第一天與最後一天不寫，由收集補抓），再用 `--verify` 抽 3 天與 API 重抓比對；其餘缺的日子由每週收集或一次性容器補完。tape 目錄由 runner 自動建立，不需事先手動建。
  - 完成後的狀態：回補完成前，步驟 3 只能用部分歷史。

- [x] **3. replay 引擎**（被擋於：步驟 1b、步驟 2）——完成 2026-11-20，見進度表；排隊估計的介面（`run_backtest(same_rate_queue_ahead=...)`、`queue_fill_rate`）已有，從 book 快照推出排隊量移到步驟 4
  - 檔案：新增 `backend/src/bfx_funding_bot/modules/backtest/replay.py` 與其測試、golden fixture；修改 `backtest/engine.py`、`backtest/config.py`、`backend/ARCHITECTURE.md`。
  - 範圍：新增 `backend/src/bfx_funding_bot/modules/backtest/replay.py`：輸入策略決策序列與成交紀錄，對每張假想掛單（利率 r、期限 P、掛出時間 t0、金額 s）輸出成交量、成交時間、閒置時間。規則：只看 t0 之後、借方主動（負號）、公開 PERIOD ≤ P（步驟 1b 第 1 節，兩種 PERIOD 語意下都成立）的成交；利率 > r 的成交依累積量成交（部分成交）；利率 = r 的成交，保守邊界不計、樂觀邊界全計，2026-07-19 之後有 book 快照時另給排隊模型估計值（可重用 `lending/tracking/queue_fill.py`）；引擎新增 replay 記帳路徑，與現有期望值路徑並存（`_apply_friction` 與 runner 的 `--fill-model` 在步驟 5 才刪，讓步驟 4 的驗證能在刪除前放行）；成交紀錄缺日時 `BacktestIncomplete`，`BacktestIncompleteReason` 新增對應原因。FRR 成交無法辨識（步驟 1b 第 5 節），不排除，報告的已知限制寫明會偏樂觀；fUST 15 天以上的期限每日成交量 p10 只有 150–472（步驟 1b 第 2 節），報告的已知限制寫明沒有市場衝擊模型。`backend/ARCHITECTURE.md` 的回測段落同步改寫。
  - 消費端：`rg -n "run_backtest\(|_apply_friction|BacktestConfig\(" backend/src backend/scripts backend/tests`。
  - 不能動：`OPTIMIZER_LIVE` 路徑（`FillRateModel`、`estimate_fill`、book learner）；WFO／DSR 計算。
  - 驗收：golden 測試（固定小段成交紀錄＋固定掛單 → 固定成交量與時間）；mutation：期限不相容的成交也算、正號成交也算、同價成交在保守邊界也算、部分成交改成全額、拿掉缺日的 `BacktestIncomplete`，各自要讓測試失敗；全量 replay 一次的耗時記入進度表。
  - 停止條件：replay 一次 WFO 掃描的耗時讓每週工作超出預算 → 回報，評估 Parquet（延後項）。

- [ ] **4. 用自己成交驗證 replay**（被擋於：無；D4＝A 已拍，門檻已預登記如下）——2026-11-20 第一次執行 FAIL（65.8%）；D6、D7 兩次事後調整與 venue 時鐘容差更正後 92.1%，只當暫時放行；正式驗收等恢復放貸後的新樣本（D7），見 `step4-validation.md`
  - 檔案：新增 `backend/scripts/report_replay_validation.py` 與其測試；彙總報告放附件 `step4-validation.md`（`docs/research/` 被 `.gitignore` 擋住，逐張明細依本機個人脈絡檔放 repo 外）。
  - 範圍：`backend/scripts/report_replay_validation.py`：(0) 從 2026-07-19 起的 book 快照推出同價排在前面的量，接成 `run_backtest` 的 `same_rate_queue_ahead`（可重用 `lending/tracking/queue_fill.py` 的 `queue_ahead_of`），D4 選 A 時排隊誤差靠它；(a) 把自己的歷史 offer（利率、期限、掛出時間、金額）丟進 replay，比較 replay 判定的成交時間與實際成交時間；(b) 保守與樂觀邊界的寬度分佈（邊界太寬代表同價成交佔比高，排隊位置是主要不確定性）。replay 時剔除自己的成交（join trade id），避免自己對照自己。報告寫進 `docs/research/`。
  - 消費端：無（新報告）。
  - 不能動：既有報告。
  - 驗收：報告產出且每個數字附指令；單元測試覆蓋指標計算（把分母換掉要失敗；不剔除自己成交要失敗）。
  - **預登記（Will 2026-11-20，開跑前寫入，跑完不改）**：
    1. 邊界涵蓋率：自己每張 offer 的實際首次成交時間落在 replay 樂觀與保守邊界的首次成交時間之間（含端點）的比例 ≥ 80%。等待上限 24h 內沒成交（被取消或到期）的 offer 也算一筆：樂觀邊界判成交、保守邊界沒判成交時算涵蓋，保守邊界判成交才算錯。
    2. 排隊估計誤差：book 快照推出的排隊估計，首次成交時間 |估計 − 實際| 的中位數 ≤ 60 分鐘；另報正負偏差（中位數，帶號）。
    3. 樣本下限：每個期限桶 ≥ 30 筆有實際成交的自己 offer；排隊估計只算掛出時刻前後 5 分鐘內有 book 快照的 offer。
    4. 未達樣本下限時只看邊界寬度放行：自己 offer 的邊界寬度（保守減樂觀的首次成交時間；保守邊界沒成交就算到 24h）中位數 ≤ 4 小時。
  - 放行規則（步驟 5 的「通過」）：依 D4 預登記的門檻。自己成交達不到樣本下限時，報告寫明「未達樣本下限」，只看 (b) 放行，(a) 由步驟 7 每週追蹤。
  - 停止條件：(a) 達樣本下限卻不過門檻 → 不進步驟 5，回報並比對業界排隊模型。

- [ ] **5. runner 改走 replay，刪除期望值記帳、linear 與 K 線 learner**（被擋於：D3、步驟 4 用新樣本通過（D7））
  - 步驟 6 已先完成（D8）：`run_decision_space_search.py` 從一開始就走 replay，不在本步改寫範圍；它從 `run_period_structure_backtest.py` import `build_arms`、`ApParams` 與預設值，改寫那支 runner 時這些名字要留著或一起搬。
  - 步驟 6b 的 review 記錄項：`run_backtest` 帶著 `noqa: C901, PLR0912, PLR0913, PLR0915, PLR0917`，6b 只把鎖倉長度抽成 `_replay_lock_ms`，沒有拆整個 replay 分支。本步刪掉期望值記帳分支時，把 replay 記帳整段拆成獨立函式、`tape`／`same_rate_queue_ahead`／`refinance_reference` 併成一個輸入物件，並把 ratchet 的 noqa 數調低。
  - 檔案：刪除與修改依盤點「去向」欄（每列已寫刪除或改寫）；另修改 `deploy/vm/ops/docker-compose.weekly-report.yml`、`deploy/vm/systemd/bfx-weekly-report.service`、`backend/ARCHITECTURE.md`、相關 ADR。
  - 範圍：照盤點「去向」欄逐列落地：刪 `compute_fill_prob`、`fill_alpha`、`fill_model` Literal 與 empirical／linear 分支、`EMPIRICAL_SOURCES`、`FillRateLearner`／`learn_fill_rate.py`、三支一次性 runner；其餘 runner 改走 replay，刪 `--fill-model`；`period_structure.py:278`、`band_sweep.py:301` 改印 replay 與邊界；每週工作刪 linear 那步（:86）、:85 與 `run_oos_profitability`（:87）改 replay，:84 book learner 保留（live artifact）；報告檔名 `*-period-structure-book.json` 改為 `*-period-structure-replay.json`，`diff_research_report` 對新檔名從零開始比對（舊檔留在 `docs/research/` 當歷史）；`ARCHITECTURE.md` 回測與 fill model 段落改寫；提到被刪機制的 ADR（`rg -ln "linear-baseline|fill_alpha|learn_fill_rate" docs/adr`）各加一行指向 ADR 2026-11-20；`BacktestIncompleteReason` 刪 `fill_model_*` 與 `_raise_for_unavailable`；quality ratchet 測試數下降時用 `Ratchet-Reason:`／`Removed-Test:` trailer。DB 既有 candle artifact 保留不刪（artifact 不可變）。
  - 消費端：`rg -ln "linear-baseline|fill_alpha|learn_fill_rate|FillRateLearner|run_phase3b_wfo_matrix|run_phase4_3|explore_mr_param|compute_fill_prob|--fill-model|EMPIRICAL_SOURCES" --hidden -g '!.git' .`
  - 不能動：book learner、`FillRateModel`、`estimate_fill`；live 不變量。
  - 驗收：`rg -n "linear-baseline|fill_alpha|compute_fill_prob|FillRateLearner|--fill-model|EMPIRICAL_SOURCES" backend deploy .claude` 無結果；`docs/` 只剩 ADR 與 `docs/research/` 的歷史文字；mutation：把任一 runner 改回期望值記帳，測試要失敗；全套單元測試綠；每週工作 :85、:87 的 timeout 依步驟 3 量到的 replay 耗時×3 重設，compose 各步 timeout 總和＋300 ≤ `TimeoutStartSec`，unit 檔預算註解同步。
  - 需要人做的事：發版核准。

- [x] **6. 決策空間搜尋 runner**（被擋於：步驟 3；D8 起不再被步驟 5 擋住）——完成 2026-11-21（PR #451），見進度表
  - 檔案：新增 `backend/src/bfx_funding_bot/modules/backtest/decision_search.py`、`backend/scripts/run_decision_space_search.py` 與兩者的測試；修改 `backend/src/bfx_funding_bot/modules/backtest/period_structure.py`（抽出 `symbol_to_json`，行為不變）、`backtest/config.py` 與 `backtest/engine.py`（新增 `replay_no_placement_tail_h`，預設行為不變）、`backend/scripts/run_oos_profitability.py`（`DEFAULT_N_TRIALS`）、`backend/ARCHITECTURE.md`。不進每週工作。
  - 範圍：`run_decision_space_search.py` 直接走 `fill_model="tape-replay"`，和舊的期望值記帳並存（舊路徑在步驟 5 才刪）。每個臂用保守與樂觀兩個邊界各 replay 一次，沿用 calendar WFO，對 `always_2d` 做逐月配對差；兩個邊界的平均差 CI 同號才下結論，否則寫 `wait_for_live`。只評測試月每一天都有成交紀錄日檔的 window；評到的 window 少於 12 個不下結論（`too_few_windows`）；等待時間不同的臂共用同一個停止掛單的時點，等得短的臂不會在月底多出可放貸的時數。兩個題目：`--topic period-arms` 是第一個預登記題目，重跑期限臂（`always_30d`、`mr_p2`、`mr_a30`、`adaptive_period`，取代 2026-09-22 linear 結果；`mr_a30_legacy` 與 `always_frr` 在 replay 下沒有意義，不跑）；`--topic grid` 在（掛價偏移 × 期限 × 沒成交時等多久再決策）上搜尋 `AlwaysMarketRate`，每次開跑前先在 registry 預登記宣告空間。報告的固定限制寫明 replay 尚未正式驗收（D7），結論不作為 promote 的依據。
  - 消費端：`rg -n "run_decision_space_search|decision_search|symbol_to_json|DEFAULT_N_TRIALS" backend docs deploy .claude`。
  - 不能動：既有 WFO／DSR 計算；`replay.py`；`run_backtest` 的既有行為（只加預設關閉的 `replay_no_placement_tail_h`）；每週工作。
  - 驗收：單元測試（兩個邊界同號才下結論、同價成交時 `wait_for_live`、等待時間短的臂掛單次數多、window 限制在成交紀錄範圍內（中間缺一天只丟那個 window）、window 太少不下結論、等待時間短但沒用到的臂和基準收益相同、缺成交紀錄標 `not_scored`、grid 參數不合法或缺序列時在開跑前報錯、trials 計數寫入報告、grid 的 `--n-trials` 低於已登記數加格點數就拒絕）；mutation 在複本跑：只跑一個邊界、verdict 只看一個邊界、拿掉 window 限制、拿掉 trials 下限檢查、`QuoteOffset` 不套偏移，各自要讓測試失敗；`DEFAULT_N_TRIALS`（`run_oos_profitability.py:53`）從 38 加上本步預登記的 4 個臂＝42；在 VM 跑出第一份報告（`period-arms`，兩幣），放 repo 外的研究筆記。
  - 需要人做的事：無（VM 一次性容器唯讀執行，程式以唯讀掛載提供，不需要發版）。

- [ ] **6b. 搜尋 runner 加上提前還款情境**（被擋於：步驟 6；D9）
  - 檔案：修改 `backend/src/bfx_funding_bot/modules/backtest/config.py`、`engine.py`、`decision_search.py`、`backend/scripts/run_decision_space_search.py` 與三者的測試、`backend/ARCHITECTURE.md`。
  - 範圍：步驟 6 的第一份報告把每筆鎖倉算成持有到期，違反 ADR 2026-09-27 的 R1（評估鎖更長的規則要先把借方利率下跌就還款算進去）。replay 記帳新增還款情境：`BacktestConfig.replay_refinance_margin`＋`run_backtest(refinance_reference=...)`，鎖倉開始後 2 天期市場收盤價第一次比鎖定利率低超過 margin 的那個小時就還款，利息算到那時、資金可以再放；規則與預設 0.2 照 ADR 2026-09-27 D3 與 `period_threshold.py` 的 `refinance_on_drop`，不另選參數。搜尋 runner 每個幣別固定報兩個情境（`held_to_maturity`、`refinance_on_drop_0.2`），各自對 `always_2d` 下結論。不算新的 trial：臂與參數不變，只多一種記帳情境（和兩個成交邊界同一類）。
  - 消費端：`rg -n "replay_refinance_margin|refinance_reference|evaluate_search_arms\(|ReplayTerms|run_symbol\(" backend`。
  - 不能動：預設行為（margin 沒設時結果逐欄相同）；`period_threshold.py`；`replay.py`；每週工作。
  - 驗收：單元測試（跌破 margin 的那個小時還款、沒跌或跌幅在 margin 內時結果與持有到期逐欄相同、margin 與參考序列必須一起給、長鎖倉的臂在持有到期為正而在還款情境為負、報告與 JSON 每個幣別兩個情境）；mutation 在複本跑：還款判斷拿掉、margin 方向寫反、鎖倉長度不縮短、runner 只跑一個情境，各自要讓測試失敗；在 VM 跑出第二份報告（`period-arms`，兩幣，兩個情境）。

- [ ] **7. 每週工作接上 replay 驗證**（被擋於：步驟 4、5）
  - 檔案：修改 `deploy/vm/ops/docker-compose.weekly-report.yml`、`deploy/vm/systemd/bfx-weekly-report.service`、`backend/tests/scripts/test_vm_weekly_report.py`。
  - 範圍：每週工作在 replay 報告之後跑 `report_replay_validation`，偏差超過 D4 門檻時報告標 DRIFT（不自動改任何東西）。
  - 消費端：`deploy/vm/ops/docker-compose.weekly-report.yml`、`tests/scripts/test_vm_weekly_report.py`。
  - 驗收：weekly report 測試涵蓋新步驟順序；compose 各步 timeout 總和＋300 ≤ `TimeoutStartSec`，unit 檔預算註解同步；mutation：拿掉 DRIFT 判斷測試要失敗。
  - 需要人做的事：發版核准。

- [ ] **8. 收尾**：完成定義逐項核對；盤點每列去向無空格；獨立設計審查（diff）；延後項附觸發條件搬回主清單；D4 的決定補進 ADR 2026-11-20 或另寫 ADR；研究 registry 把 linear 與期望值記帳時代的結果全部降為「待 replay 重跑」；刪除 VM 上步驟 1 的一次性目錄。驗收：`rg` 指令（步驟 5）無結果、CI 綠、`docs/research/` 有 replay 驗證與第一份搜尋報告。

## 決定

- **D1 成交紀錄存哪裡**（狀態：已決 2026-11-20，Will）：每日 gzip CSV 檔，放 VM，不進 Postgres；不另存 R2（D5）。ADR 2026-11-20 D2。
- **D2 回測怎麼判定成交**（狀態：已決 2026-11-20，Will）：逐筆 replay，取代查表成交機率 × 利率；原 D2「排隊量從哪來」只剩同價成交的排隊模型，併入步驟 3。ADR 2026-11-20 D1。
- **D3 linear 時代的研究結論怎麼處理**（狀態：已決 2026-11-15，Will）：registry 全部標「待重跑」，程式刪 linear；DSR trials 累計保留。期望值記帳時代的結果比照處理（2026-11-20 隨 D2 擴大）。
- **D4 replay 驗證的判準**（狀態：已決 2026-11-20，Will：A；原擋住步驟 4、7。門檻數字與樣本下限在步驟 4 開跑前預登記，步驟 8 補進 ADR）
  - A：自己 offer 的實際成交時間落在 replay 保守與樂觀邊界之間的比例 ≥ 預登記門檻，且排隊模型估計值的誤差中位數 ≤ 預登記門檻。（建議；門檻數字在步驟 3 完成後、步驟 4 開跑前預登記）
  - B：只看邊界涵蓋率——簡單；代價是不檢驗排隊模型。
  - 兩案都要預登記樣本下限（建議：每個期限桶 ≥30 筆自己的成交）。

- **D5 成交紀錄要不要另存一份到 R2**（狀態：已決 2026-11-20，Will：A，不另存；2026-11-20 第 2 輪計畫審查提出）
  - 事實：既有 offsite 工具是 pgBackRest，只備份資料庫，不能上傳一般檔案；每週容器不帶任何秘密；標準函式庫做不到 S3 簽章。成交紀錄可以從 Bitfinex 公開 API 重抓（完整回補約 3.5 天）。
  - A：不另存，VM 上的日檔就是唯一副本；VM 毀損時從 API 重抓。不新增依賴、不碰憑證。代價：重建要 3.5 天；若 Bitfinex 某天不再提供舊歷史，就再也拿不回來。（建議；延後觸發：Bitfinex 縮短歷史可查範圍，或研究需要在 VM 重建後一天內恢復。）
  - B：主機上的 systemd unit 用 VM 已有的 CLI 上傳，另發一把只限新前綴的 R2 token（Will 設定）。代價：新秘密檔、新 unit、與 DR bucket 共用帳戶。
  - C：研究容器內用 Python 上傳，要新增 S3 客戶端依賴（需 owner 同意）。
  - 結論：A；ADR 2026-11-20 B1 已同步改寫，另存 R2 列入延後並附觸發條件。

- **D6 步驟 4 第一次執行 FAIL 之後怎麼辦**（狀態：已決 2026-11-20，Will：照建議）：門檻不變；補 replay 模型與量法後，用同一批 offer 重跑，報告標「事後調整」，通過也只當暫時放行，正式驗收等恢復放貸後的新樣本（延後項「用 live 成交做校準」與步驟 7）。補的內容：掛出時間改用 venue ack（`transport_outcome_journal.completed_at_ms`），涵蓋判定容差 1 秒（tape 時間戳是整秒）；只剔除「其他」自己 offer 的成交，被 replay 的那張自己的成交留作需求（同價，只影響樂觀邊界與排隊估計）；`replay_offer(crossing_bid_amount=...)`：掛出前快照裡會被穿過的 bid 立即成交；實際首次成交與取消時間另取掛單歷史（`bfx_operator` 唯讀匯出，Will 核准），取消的 offer 只 replay 到取消時刻，沒成交又查不到歷史的 offer 排除並計數。第一次執行的結果保留在 `step4-validation.md`。

- **D7 第 2 輪 review 找到量法 blocking 之後怎麼辦**（狀態：已決 2026-11-20，Will：照建議）：同意第三輪修正，修完 5 項 blocking 與 1 項 should 後重跑同一批 38 張，標「第二次事後調整」，只當參考；模型改進照樣合進 `dev`。步驟 5 的放行條件改成：恢復放貸後，用新的自己成交（至少 30 張、量法在開跑前凍結於本分支的 `report_replay_validation.py`）重跑並通過同一組門檻。

- **D8 步驟 6 要不要等步驟 5**（狀態：已決 2026-11-21，Will：提前）：步驟 6 需要的是 replay 引擎（步驟 3），不是「舊方法已刪除」；D7 的新樣本驗收只擋步驟 5（刪舊方法、每週工作改走 replay）。步驟 6 提前到步驟 5 之前，搜尋 runner 直接走 `tape-replay`、與舊路徑並存。代價：這段期間的搜尋結論建立在尚未正式驗收的 replay 上，registry 一律標「replay 未正式驗收」，不作為 promote 的依據；步驟 4 用新樣本驗收沒過時，這批結論跟著作廢重跑。

- **D9 第一份搜尋報告之後要不要補提前還款情境**（狀態：已決 2026-11-21，Will：補）：第一份報告四個臂對 `always_2d` 在兩個邊界都為正，但鎖倉算持有到期。補上 ADR 2026-09-27 D3 的還款情境（步驟 6b），用同一條規則與同一個 20% 門檻，不重新選參數；借方實際行為仍未量測（要 live 的長期限樣本，見該 ADR 的 Revocation Triggers）。

拍板順序：D1–D9 全部已拍。

## 進度

狀態值：未開始／進行中／程式完成待驗收／完成。

| 步驟 | 狀態 | 已跑的驗收 | 未跑的驗收與原因 | commit |
|---|---|---|---|---|
| 1 Spike 完整量測 | 完成 | 全量抓取兩幣、`--summarize`、一週增量量測、PG18 每列大小；基線（`feaa9743`）：單元 5415 passed、mypy、ruff、quality ratchet 全綠 | 無 | PR #423 |
| 1b replay 前置事實 | 完成 | 正式 DB 唯讀匯出（Will 核准）、`period_join.py`、`replay_cost.py` 兩幣；停止條件 (0) 觸發，Will 選 A | 無 | 本 PR |
| 2 每日檔案與收集 | 完成 | 單元 5469 passed（`-n auto`）、`mypy src/`、ruff、quality ratchet；mutation 27 個全殺（`abs()` 加回、研究 service 可寫 tape、未結束當日寫檔、429/403/ratelimit body 重試、同 MTS 去重、覆寫既有日檔、空日寫檔、匯入頭尾日、缺鎖、連續失敗不停等）；compose policy 測試以本機 docker compose render；`/code-review` 與契約審查兩輪：findings 13（blocking 1），fixed 11、recorded 2（nit：`--dry-run` 也會建 tape 目錄；chown 失敗會留下 root 擁有的目錄），rejected 0 ；CI（含 integration）綠；發版 PR #428 部署 `ede0482b`（`migrations=none`，ledger #530）；VM 上 `--import-csv` 兩幣各 1,778 天（2022-01-02..2026-11-14，來源頭尾日依設計不寫），`--verify` 抽 2022-06-15、2024-09-03、2026-11-14 兩幣 6 個日檔全部一致，再以收集補上 2022-01-01 與 2026-11-15..19；目前兩幣各 1,784 天、無缺日，共 1.2 GB | 每週 `tape-ingest` 的第一次實跑（下週一 04:17 UTC） | PR #427、#428 |
| 3 replay 引擎 | 完成 | 單元 5492 passed（`-n auto`）、`mypy src/`、ruff、quality ratchet；golden 測試；mutation 3 輪共 31 個，非等價的全殺（期限不相容、正號、同價進保守、部分成交改全額、拿掉缺日 Incomplete、t0 含當下、偷看當小時成交、等待截短當成沒成交、閒置未截在 window 內、float 容差、排序檢查、排隊量等；存活 1 個是等價 mutant：負號篩過後 `-x`＝`abs(x)`）；`/code-review` 兩輪＋契約審查一輪：findings 21（blocking 0），fixed 19、recorded 2（K 線缺小時時 cooldown 以根數計的既有假設；從 book 快照推排隊量移到步驟 4），rejected 0；全量 replay 耗時（VM 一次性容器，`spike/replay_bench.py`，每小時一張 2 天期掛單、等 24h、兩個邊界）：fUST 42,768 張 88.7 秒、fUSD 225.8 秒，合計約 314 秒／設定，RSS 峰值 489 MB；期限四臂約 1,256 秒，在每週 1,800 秒內，停止條件未觸發 | CI（含 integration）待 PR | 本 PR |
| 4 replay 驗證 | 進行中（程式完成；同批 38 張事後調整後 92.1%＝35/38（venue 時鐘容差更正後），只當參考；正式驗收等新樣本，D7） | 預登記；第一次執行 FAIL（65.8%）；D6 重跑 81.6% 經第 2 輪 review 判為不可採信；D7 修正後重跑 89.5%（排隊誤差中位數 0 分），見 `step4-validation.md`；單元、`mypy src/`、ruff；mutation 第一輪 16、D6 20、D7 10 個，全殺；`/code-review` 第 1 輪 findings 8：修 3、記錄 1、D6 處理 4；第 2 輪 findings 8（blocking 5）：D7 修 7（5 blocking、should、nit `keep`）、nit 缺日檢查也修；第 3 輪 findings 7（blocking 4）：修 3、記錄 2、駁回 2，修完重跑仍 89.5%；單元 5534 passed（`-n auto`） | CI（PR 待開）；新樣本驗收（恢復放貸後） | 本分支 |
| 5 刪期望值記帳與 linear | 未開始 | | | |
| 6 決策空間搜尋 | 完成 | 單元 5597 passed（`-n auto`，第 2 輪修正前的 HEAD；之後只改 runner 與其測試，受影響的 3 個測試檔重跑通過）、`mypy src/`、ruff、lint-imports、quality ratchet（沒有新增 suppression）；`/code-review` 一輪＋契約審查一輪：findings 12（blocking 0），fixed 11、rejected 1（nit：runner 測試讀 repo 的 cells 設定檔，和既有的 `test_run_period_structure_backtest.py` 同一種做法）；mutation 21 個全殺（其中 1 個第一次存活，原因是測試的參數被 argparse 先擋掉，修測試後殺掉）；第一份報告 2026-11-21 在 VM 一次性容器跑完（`period-arms`，兩幣各 50 個測試月 2022-05..2026-06，p30 K 線只到 2026-07-19；約 14 分鐘）：4 個臂對 `always_2d` 在兩個邊界都是正的，但鎖倉算持有到期、沒有提前還款情境（ADR 2026-09-27 D3），長期限的結果偏樂觀，報告原文與判讀放 repo 外的研究筆記 | 無（CI 在 `e34c9636` 綠） | PR #451 |
| 6b 提前還款情境 | 進行中 | | | |
| 7 每週驗證 | 未開始 | | | |
| 8 收尾 | 未開始 | | | |
