# 盤點：成交模型與研究 runner（快照 `a1452398`，2026-11-15）

產生方式：唯讀盤點，指令列在文末。每一列有「去向」；收尾時不得有空格。

## 成交模型實作與選擇點

| 項目 | 位置 | 現況 | 去向 |
|---|---|---|---|
| `fill_model` Literal（`empirical`／`linear-baseline`） | `backend/src/bfx_funding_bot/modules/backtest/config.py:26` | 預設 `empirical` | 只留 `empirical`（步驟 5） |
| `fill_alpha` | `config.py:22`，用在 `engine.py:148`、`period_structure.py:272,329` | linear 斜率 | 刪除（步驟 5） |
| `compute_fill_prob` | `config.py:51-65` | `max(0, 1 - alpha*spread)` | 刪除（步驟 5） |
| `EMPIRICAL_SOURCES`（`{"candle","book"}`） | `backtest/engine.py:25-28,74,90`；`ARCHITECTURE.md:361` | source 白名單 | 加 `tape`（步驟 3）、移除 `candle`（步驟 5） |
| engine 分派 | `backtest/engine.py:124-148,162-165` | empirical 缺模型時 raise `BacktestIncomplete` | 保留；刪 linear 分支（步驟 5） |
| `FillRateModel` | `lending/tracking/model.py:59`（`estimate_fill` :117） | stats＋artifact 內插 | 保留，介面不變；新增 `source="tape"`（步驟 3） |
| `FillRateLearner`（K 線） | `lending/tracking/fill_rate.py:52`；`scripts/learn_fill_rate.py` | 從 K 線學 | 刪除（步驟 5；tape 取代，K 線無法看到成交） |
| `BookReplayLearner` | `lending/tracking/book_replay.py:45`；`scripts/learn_book_fill_rate.py` | 每週產出 artifact（VM 24 份，2026-09-27..11-09） | 保留，當 tape 模型的排隊折扣與對照（步驟 3、4） |
| `queue_fill` helpers | `lending/tracking/queue_fill.py:19,28` | book 排隊 | 保留 |
| `fill_rate_model_artifacts`／`fill_rate_stats` | `lending/tracking/tables.py:23,49` | 存 artifact 與 stats | 保留；`source` 加 `tape` |
| period_structure 報告字樣 | `backtest/period_structure.py:278` | 寫死 linear 說明 | 改為印實際 model source（步驟 5） |
| band_sweep 寫死 fill=1 | `backtest/band_sweep.py:301,308` | 報告寫 mean fill = 1.0 | 改走 empirical（步驟 5） |
| simulated_venue market_replay | `simulated_venue/_internal/market_replay.py` | 模擬交易所的成交路徑，非研究用 | 不動（不在範圍；live parity 用） |

## 研究 runner（`backend/scripts/`）

| runner | 現況成交模型 | 去向 |
|---|---|---|
| `run_backtest` :80 | linear | 改 empirical（步驟 5） |
| `run_oos_profitability` :60,:242 | linear | 改 empirical（步驟 5） |
| `run_adaptive_band_sweep` :80 | linear＋報告 fill=1 | 改 empirical（步驟 5） |
| `run_distortion_sensitivity` :118 | linear | 改 empirical（步驟 5） |
| `run_phase3b_wfo_matrix` :55,:131 | linear | 刪除（步驟 5；一次性研究，結果已在報告） |
| `run_phase4_3_locf_wfo_matrix` :63 | linear | 刪除（同上） |
| `run_frr_floor_backtest` :518 | 只有 linear | 改 empirical（步驟 5） |
| `run_period_structure_backtest` :326 | linear 預設，`--fill-model book` | 只留 empirical，`--fill-source tape\|book`（步驟 5） |
| `explore_mr_param_sensitivity` :40 | linear | 刪除（一次性探索） |
| `derive_cells` :43-44 | 傳空 `FillRateModel` 不使用 | 改用 tape 模型（步驟 5） |
| `learn_fill_rate` | K 線 learner | 刪除（步驟 5） |
| `learn_book_fill_rate` | book learner | 保留 |
| 其他 7 支（g2／g3／threshold／signal_eda／attribution／eda_phase3b／diff_research_report） | 不用成交模型 | 不動 |

## Live money path（不變量依據）

- `DeploymentReconciler` 的 `fill_model_provider`（`execution/deployment/reconciler.py:155-164,195,715-736`）；`apps/bot.py:761` 建 reconciler 時不傳，恆為 None。
- `BFX_FILL_MODEL_ARTIFACT`（`apps/config.py:140`）只在 `OPTIMIZER_LIVE` 要求，沒有其他讀者。
- live 樹不 import `FillRateModel` 或 repository loader。

## 研究資料表與權限

- `funding_candles`、`funding_book_snapshots`（VM：2026-07-19 起 17,287 筆）、`funding_stats`、`funding_trades`（自己的成交，64 筆，`maker` 全 null）。沒有市場成交紀錄表。
- `bfx_research` 權限：migration `alembic/versions/3c5e7a9b1d2f_research_role_allowlist.py:57-74`，鏡像測試 `tests/integration/test_research_privilege_allowlist.py:42-61`。

## 每週研究工作

`deploy/vm/ops/docker-compose.weekly-report.yml:74-89` 共 13 步；成交相關：`learn_book_fill_rate`（:84）、period-structure book（:85）、period-structure linear（:86）、`run_oos_profitability`（:87）。去向：:86 刪除、:87 改 empirical、新增 tape 增量收集與 `learn_tape_fill_rate`（步驟 2、3、5）。

## 測試

backtest 16 檔、tracking 9 檔（含 `book_replay_golden.json`）、scripts 6 檔、live 5 檔、其他 5 檔；清單用下面第 6 條指令重產。

## 指令

1. `rg -n "fill_model|fill_alpha|FillRateModel|fill_rate_model_artifacts|fill_rate_stats|BOOK_SOURCE|fill_prob|learn_book_fill_rate|linear-baseline" backend/src backend/scripts deploy`
2. `rg -n "fill_model_provider|estimate_fill" backend/src`
3. `rg -n "DeploymentReconciler\(" backend/src`
4. `rg -n '__tablename__ = "funding_' backend/src`
5. `rg -il "public.*trade|trades_public|funding_public" backend/src`（無結果）
6. `rg -l "fill_model|FillRateModel|fill_rate|fill_prob|book_replay|fill_alpha" backend/tests`
