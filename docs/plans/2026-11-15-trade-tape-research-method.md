# 策略研究改以市場成交紀錄為地基

**Goal：** 每個候選策略都在「以 Bitfinex 公開 funding 成交紀錄學出、並用自己的成交校準過」的成交模型上評估；`linear-baseline` 與 K 線成交 learner 從程式移除；每週研究工作自動收集成交紀錄、更新模型、輸出校準偏差。驗收見步驟 8。

**上層：** 主清單見本機個人脈絡檔（`AGENTS.local.md`）。

**附件：** `2026-11-15-trade-tape-research-method/`（`inventory.md` 盤點，快照 `a1452398`）。

## 全域約束與完成定義（暫代主計畫）

- live money path 不得讀取本計畫的任何產物（新表、`source="tape"` 的 artifact）；本計畫只改研究路徑與每週研究工作。
- 不新增依賴；需要時先列套件與版本等 owner 同意（`AGENTS.md`「供應鏈與依賴」）。
- 每個新 sweep／回測照既有預登記規則：開跑前登記 mechanism 與宣告空間，DSR trials 累計同步 `run_oos_profitability.py` 的 `DEFAULT_N_TRIALS`。
- 完成定義：盤點每列去向已落地；新增行為有「破壞新行為要失敗」的測試；`uv run pytest -m "not integration"`、`mypy`、`ruff`、quality ratchet 綠；integration 由 CI 跑。

## 前提（開工時核對過，2026-11-15）

- 會改到的既有部署是否在線：**在線**（VM，`bfx-app` 與每週 `bfx-weekly-report`）。會碰到正式環境的只有步驟 2 的 migration（純新增一張表與 `bfx_research` 授權）與每週工作的 compose 改動：回滾＝`alembic downgrade` 到前一版（drop 新表）＋revert compose；沒有相容性窗口需求，因為 live bot 不讀這些東西（見不變量）。
- 公開 `GET /v2/trades/f{SYM}/hist` 回傳全市場成交 `[ID, MTS, AMOUNT, RATE, PERIOD]`，fUST 有 2022-01 起的資料（實測，2026-11-15；已確認）。
- AMOUNT 正負號＝主動成交方：負＝借方主動吃放貸掛單，正＝放貸方主動吃借方掛單（4 筆自己成交以 offer 掛出到成交時間差對照，2026-11-15；已確認，樣本小）。私有 `auth/r/funding/trades` 的 AMOUNT 恆正、MAKER 恆 null（實測，已確認）。規則寫在 `.claude/rules/bitfinex-venue.md` 的 PR #418（未合併；擋住步驟 3）。
- 私有 funding trades 的 trade id 與公開成交紀錄的 ID 是同一個值：自己的 434229991、434203048、434032545、433811878 在公開 `/v2/trades/fUST/hist` 以 ID 直接找到（實測 2026-11-15；已確認），所以剔除自己成交可用 trade id join。
- 公開成交紀錄的 trade id 是跨 symbol 共用的全域序號（推論，未實測；步驟 1 驗），同 symbol 相鄰 id 跳號不代表漏資料。
- 密度：fUST 2022-01..2024-08 抽 6 天，每天 4.8k–32k 筆，p30 每天 0–414 筆，負號 93–99%（實測，已確認）。fUSD 未抽到（撞 429；未確認）。
- `api-pub.bitfinex.com` 對 Python 預設 UA 回 403，需通用 UA；分頁連打約 30 次後 429（實測）。
- 每週研究工作現有各步 timeout 合計 5100 秒，`TimeoutStartSec=5400` 只剩 300 秒（`deploy/vm/systemd/bfx-weekly-report.service:20-25` 註解；已確認 2026-11-15），新增步驟預期必須調高 `TimeoutStartSec`。
- VM 磁碟剩 114G，DB 3.1 GB（實測 2026-11-15）。
- book 成交模型每週已產出（`fill_rate_model_artifacts` source=book 24 份，2026-09-27..11-09），但 book 快照只從 2026-07-19 起（實測）。
- 已報告的研究數字（期限回測 2026-09-22、OOS）用 `linear-baseline`（`backtest/period_structure.py:278`；已確認）。

**驗收基線：** 步驟 1 開工時在 `a1452398` 跑 `uv run pytest -m "not integration" -n auto`、`mypy src/`、`ruff check`、`quality_ratchet.py`，結果記入進度表；既有失敗不在本計畫修。

## 本階段依賴的不變量

| 不變量 | 依賴它的機制 |
|---|---|
| live bot 不建 `FillRateModel`、不讀成交模型表 | `apps/bot.py:761`（reconciler 不傳 `fill_model_provider`）、`apps/config.py:140`（`BFX_FILL_MODEL_ARTIFACT` 只在 `OPTIMIZER_LIVE`）、`execution/deployment/reconciler.py:715` |
| `estimate_fill(reference_rate, offer_rate, period_agg, horizon_h)` 簽名不變 | `lending/tracking/model.py:117`、`backtest/engine.py:140`、reconciler 的 `FillModelEvidenceProvider` protocol（:155-164） |
| artifact 建立後不可變、不跨 scope 重用 | `lending/tracking/artifact.py`、`repository.py:53 ensure_fill_model_artifact` |
| `bfx_research` 權限是精確 allowlist，migration 與測試鏡像 | `alembic/versions/3c5e7a9b1d2f_research_role_allowlist.py`、`tests/integration/test_research_privilege_allowlist.py` |
| 每週研究工作各步 timeout 總和 < systemd `TimeoutStartSec=5400` | `deploy/vm/systemd/bfx-weekly-report.service:25`、`deploy/vm/ops/docker-compose.weekly-report.yml:74-89` 各步 `timeout` |
| backtest engine 只接受登記過的 empirical source | `backend/src/bfx_funding_bot/modules/backtest/engine.py:25-28,74,90`（`EMPIRICAL_SOURCES`）、`backend/ARCHITECTURE.md:361` |
| 成交紀錄以 venue trade id 為唯一身分 | 步驟 2 的表主鍵（symbol, trade_id）、步驟 3 剔除自己成交的 join（`funding_trades.trade_id`） |

## 範圍

- 做：成交紀錄回補與每週增量收集（fUSD、fUST）；tape 成交 learner（按天期分桶、只用負號成交做 trade-through、剔除自己成交、可選 book 排隊折扣）；校準報告；runner 全面改走 empirical 並刪除 linear 與 K 線 learner；決策空間搜尋 runner；每週工作接上。
- 不做：live money path 使用成交模型（`OPTIMIZER_LIVE`）——那是另一個決定，要有本計畫的校準結果才談；fUSD／fUST 以外的幣（目前不放貸）；bandit 資金分配（本金小，與 A/B 搶樣本）。
- 延後：
  - 用 live 成交做校準的正式驗收門檻（觸發：恢復放貸且自己的成交 ≥ D4 的樣本下限）。
  - bandit 資金分配（觸發：本金放大到 cap 加碼 gate 通過）。

## 步驟

- [ ] **1. Spike：完整成交紀錄量測**（被擋於：無）
  - 範圍：唯讀腳本，fUSD 與 fUST 都從 2022-01-01 完整分頁抓到本機暫存目錄（資料不進 repo、不寫 DB）。輸出：每日筆數、各天期分桶筆數、正負號比例、時間空窗（同 symbol 相鄰成交間隔的分佈與最長空窗）、全域 id 序號假設的驗證（兩個 symbol 的 id 是否交錯）、總列數與估計 DB 大小、完整抓取耗時、一週增量的抓取秒數、429 次數。
  - 檔案：新增 `2026-11-15-trade-tape-research-method/spike/fetch_tape.py` 與 `spike/report.md`（commit）；抓下的資料不 commit。
  - 消費端：無（spike）。
  - 不能動：任何 repo 程式與正式 DB。
  - 驗收：`spike/report.md` 附上述每個數字與產生它的指令。
  - 停止條件：任何 symbol 有超過 7 天沒有任何成交的空窗 → 回報，D1／D2 重評。

- [ ] **2. 成交紀錄表與收集**（被擋於：D1、步驟 1）
  - 範圍：新表（D1 決定形式；預設 `funding_public_trades`：symbol、trade_id、mts、amount（帶正負號）、rate、period，主鍵 (symbol, trade_id)，索引 (symbol, period, mts)）；migration 同時授權 `bfx_research` SELECT/INSERT 並更新鏡像測試；`scripts/ingest_public_trades.py`：從 DB 現況推導續抓起點，冪等（`ON CONFLICT DO NOTHING`），可中斷續跑，節流與 429 退避，通用 UA；每週工作在 `ingest_funding_stats` 之後加一步增量收集。
  - 消費端：`rg -n "funding_public_trades|ingest_public_trades" backend deploy`（新增，無既有消費端）；權限鏡像 `tests/integration/test_research_privilege_allowlist.py`。
  - 不能動：既有表與 `bfx_research` 其他權限。
  - 驗收：單元測試（分頁續抓起點、重跑不重複、429 退避、正負號原樣保存——把 `abs()` 加回去測試要失敗）；integration（migration up/down、權限 allowlist）由 CI 跑；`alembic check` 無 drift。
  - 停止條件：步驟 1 估計大小超過 VM 剩餘磁碟 10% → 回報 D1。
  - 每週增量步驟的 `timeout` 依步驟 1 的「一週增量秒數」×3 設定，同一個 PR 把 `TimeoutStartSec` 調到新合計＋300 秒餘裕，並同步改 unit 檔的預算註解；驗收核對 compose 各步 timeout 總和＋300 ≤ `TimeoutStartSec`。
  - 需要人做的事：發版（`dev`→`main` PR 合併＝部署，需 Will 核准）；發版後在 VM 用 weekly-report 的 compose（`bfx_research` role，`/opt/bfx/runtime/research.env`）以 `docker compose -f deploy/vm/ops/docker-compose.weekly-report.yml run --rm <service> python -m scripts.ingest_public_trades --from 2022-01-01` 跑完整回補；中斷後重跑同一指令，從 DB 現況續抓。
  - 完成後的狀態：回補完成前，步驟 3 只能用部分歷史。

- [ ] **3. tape 成交 learner**（被擋於：D2、步驟 2、PR #418 合併）
  - 範圍：新增 `lending/tracking/tape_fill.py` 的 learner，輸出既有 `fill_rate_stats` 形狀、`source="tape"` 的 artifact；修改 `backtest/engine.py` 的 `EMPIRICAL_SOURCES` 加入 `tape`，`ARCHITECTURE.md:361` 同步。定義：對報價 r、天期桶 p、horizon h，fill_prob＝在掛出後 h 小時內，同天期桶出現 rate ≥ r 的負號成交且累積量超過估計排隊量的窗比例（D2 決定排隊量來源）。剔除自己的成交（join `funding_trades.trade_id`）；正號成交不算 trade-through。`scripts/learn_tape_fill_rate.py`。
  - 消費端：`rg -n "FillRateModel.from_rows|load_fill_model_artifact|source=" backend/src backend/scripts`。
  - 不能動：`estimate_fill` 簽名、artifact 不可變性、book learner 的行為與 golden。
  - 驗收：golden 測試（固定小段成交紀錄 → 固定 stats）；engine 以 tape artifact 跑完一次 empirical 回測的測試；mutation：把正號成交算進 trade-through、不剔除自己成交、天期不分桶、`EMPIRICAL_SOURCES` 拿掉 tape，各自要讓測試失敗。
  - 停止條件：p30 桶在任一 calendar WFO 窗的樣本不足以產出 high-confidence 模型 → 回報，D2 決定是否改週彙總或參數化平滑。

- [ ] **4. 校準報告**（被擋於：D4、步驟 3）
  - 範圍：`scripts/report_fill_calibration.py`：(a) tape 模型 vs book 模型在 2026-07-19 之後重疊期的逐桶 fill_prob 差；(b) tape 模型預測 vs 自己 64 筆成交的實際成交時間（offer 掛出到成交）；指標由 D4 決定。報告寫進 `docs/research/`。
  - 消費端：無（新報告）。
  - 不能動：既有報告。
  - 驗收：報告產出且每個數字附指令；單元測試覆蓋指標計算（把指標的分母換掉要失敗）。
  - 放行規則（步驟 5 的「通過」）：(a) 過門檻，且 (b) 在自己成交 ≥ D4 樣本下限時也過門檻；(b) 樣本不足時只看 (a) 放行，(b) 改由步驟 7 每週追蹤，報告寫明「(b) 未達樣本下限」。
  - 停止條件：(a) 不過，或 (b) 達樣本下限卻不過 → 不進步驟 5，回報並比對業界排隊模型修正。

- [ ] **5. runner 改走 empirical，刪除 linear 與 K 線 learner**（被擋於：D3、步驟 4 通過）
  - 範圍：照盤點「去向」欄逐列落地：刪 `compute_fill_prob`、`fill_alpha`、`linear-baseline` 分支、`FillRateLearner`／`learn_fill_rate.py`、三支一次性 runner；`EMPIRICAL_SOURCES` 移除 `candle`（DB 既有 candle artifact 保留不刪，artifact 不可變）；其餘 runner 改 empirical，`--fill-model` 旗標改為 `--fill-source tape|book`；`period_structure.py:278`、`band_sweep.py:301` 改印實際模型；每週工作刪 linear 那步、`run_oos_profitability` 改 empirical；報告檔名 `*-period-structure-book.json` 保留不改，新增 `*-period-structure-tape.json`，`diff_research_report` 兩個 glob 各一份（歷史比對不斷）；`ARCHITECTURE.md:361,861` 改寫；提到被刪機制的 ADR（`rg -ln "linear-baseline|fill_alpha|learn_fill_rate" docs/adr`）各加一行 Superseded 指向本計畫的 ADR；quality ratchet 測試數下降時用 `Ratchet-Reason:`／`Removed-Test:` trailer。
  - 消費端：`rg -ln "linear-baseline|fill_alpha|learn_fill_rate|FillRateLearner|run_phase3b_wfo_matrix|run_phase4_3|explore_mr_param|compute_fill_prob|--fill-model" --hidden -g '!.git' .`（2026-11-15 約 20 個測試檔＋ARCHITECTURE.md＋ADR＋weekly compose＋`tests/scripts/test_vm_weekly_report.py`）。
  - 不能動：book learner；live 不變量；既有報告檔名。
  - 驗收：`rg -n "linear-baseline|fill_alpha|compute_fill_prob|FillRateLearner|--fill-model" backend deploy .claude` 無結果；`docs/` 只剩 ADR 與 `docs/research/` 的歷史文字；mutation：把任一 runner 改回 linear 預設，測試要失敗；全套單元測試綠。
  - 需要人做的事：發版核准。

- [ ] **6. 決策空間搜尋 runner**（被擋於：步驟 5）
  - 範圍：`scripts/run_decision_space_search.py`：在 tape 模型上搜尋（掛價偏移 × 天期 × 改價規則），沿用 calendar WFO、DSR、預登記；第一個預登記題目是重跑期限四臂（取代 2026-09-22 linear 結果）。
  - 消費端：無（新 runner）。
  - 不能動：既有 WFO／DSR 計算。
  - 驗收：單元測試（窗切分、trials 計數寫入報告）；`DEFAULT_N_TRIALS`（`run_oos_profitability.py:53`，現值 38）加上本步預登記的臂數；跑出第一份報告進 `docs/research/`。

- [ ] **7. 每週工作接上校準偏差**（被擋於：步驟 4、5）
  - 範圍：每週工作在 learner 之後跑 `report_fill_calibration`，偏差超過 D4 門檻時報告標 DRIFT（不自動改任何東西）。
  - 消費端：`deploy/vm/ops/docker-compose.weekly-report.yml`、`tests/scripts/test_vm_weekly_report.py`。
  - 驗收：weekly report 測試涵蓋新步驟順序；compose 各步 timeout 總和＋300 ≤ `TimeoutStartSec`，unit 檔預算註解同步；mutation：拿掉 DRIFT 判斷測試要失敗。
  - 需要人做的事：發版核准。

- [ ] **8. 收尾**：完成定義逐項核對；盤點每列去向無空格；獨立設計審查（diff）；延後項附觸發條件搬回主清單；ADR（D1–D4）寫進 `docs/adr/`；研究 registry 記錄 linear 時代結果全部降為「待 tape 重跑」。驗收：`rg` 指令（步驟 5）無結果、CI 綠、`docs/research/` 有校準與第一份搜尋報告。

## 決定

- **D1 成交紀錄存哪裡**（狀態：未決；擋住步驟：2；需要的事實：步驟 1 的總列數與大小）
  - A：Postgres 新表，走既有研究 role、備份與 migration——與其他研究資料同一套權限與查詢方式；代價是 DB 與 R2 備份變大（估數 GB 量級，待步驟 1）。（建議）
  - B：Parquet 檔放 VM 與 R2——大量歷史掃描較快、DB 不膨脹；代價是另一套存取與權限、研究容器要掛檔案、與現有 `FillRateModel` 從 DB 載入的路徑不同。
- **D2 排隊量從哪來**（狀態：未決；擋住步驟：3；需要的事實：步驟 1 的 p30 密度）
  - A：純 trade-through＋固定排隊比例參數，2026-07-19 之後用 book 快照校正該參數——全歷史都能用；代價是 2026-07 前排隊量是假設。（建議）
  - B：只用有 book 快照的期間（2026-07-19 起）做完整排隊模擬——最精確；代價是只有 4 個月歷史，回答不了 2022 起的期限問題。
  - C：參數化成交強度 λ(δ)=A·e^{−kδ}（做市文獻），稀疏桶用它平滑——樣本少時穩；代價是函數形式假設。可與 A 併用於 p30。
- **D3 linear 時代的研究結論怎麼處理**（狀態：未決；擋住步驟：5、8）
  - A：registry 全部標「待 tape 重跑」，程式刪 linear；DSR trials 累計保留（試過就算）。（建議）
  - B：保留 linear 當敏感度下限繼續報告——多一條對照；代價是兩套模型長期並存，正是要清掉的技術債。
- **D4 校準一致的判準**（狀態：未決；擋住步驟：4、7）
  - A：逐天期桶比較 fill_prob，tape 與 book 差的絕對值中位數 ≤ 預登記門檻，且自己成交的預測成交時間落在模型 80% 區間內的比例在 [70%, 90%]——兩個獨立對照都要過。（建議；門檻數字在步驟 3 完成後、步驟 4 開跑前預登記）
  - B：只用自己成交——最直接；代價是目前 64 筆，檢定力不足，且在達樣本下限前步驟 5 無法放行。
  - 兩案都要預登記 (b) 的樣本下限（建議：每個天期桶 ≥30 筆自己的成交）。

拍板順序：D3 可以現在拍；D1、D2 等步驟 1 的數字；D4 等步驟 3。

## 進度

狀態值：未開始／進行中／程式完成待驗收／完成。

| 步驟 | 狀態 | 已跑的驗收 | 未跑的驗收與原因 | commit |
|---|---|---|---|---|
| 1 Spike 完整量測 | 未開始 | | | |
| 2 表與收集 | 未開始 | | | |
| 3 tape learner | 未開始 | | | |
| 4 校準報告 | 未開始 | | | |
| 5 刪 linear | 未開始 | | | |
| 6 決策空間搜尋 | 未開始 | | | |
| 7 每週校準 | 未開始 | | | |
| 8 收尾 | 未開始 | | | |
