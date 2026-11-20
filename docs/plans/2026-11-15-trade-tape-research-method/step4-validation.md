# 步驟 4：用自己 offer 驗證 replay（第一次執行，2026-11-20）

門檻在開跑前預登記（計畫步驟 4，Will 2026-11-20），跑完沒改。逐張 offer 的明細（利率、金額、時間）不進 repo，位置記在本機個人脈絡檔。

## 怎麼跑的

- 輸入：`submission_attempt_journal`（掛出時間 `started_at_ms`、`match_rate`、`match_period_days`、`intended_amount`）join `transport_outcome_journal`／`execution_resolution_journal` 取 venue offer id；實際首次成交取 `funding_trades` 同 offer 的最早 `mts_create`；book 快照取 `funding_book_snapshots`。全部用 `bfx_research`、READ ONLY transaction（Will 2026-11-20 核准）。
- 先跑計數：日誌從 2026-09-21 起 40 次掛單，38 張拿得到 offer id，對上 38 筆成交（等於該日之後的全部成交），39 張在掛出前後 5 分鐘內有快照，全部 2 天期。
- 正式報告：VM 一次性容器（runbook `research-one-shot-jobs.md` 的 pattern，image 用 bot 的 digest，本分支的 `backend/src`、`backend/scripts` 唯讀掛入，tape 唯讀掛入），`python -m scripts.report_replay_validation --tape-root /tape --out /out/report.md`，5.4 秒。

## 結果：FAIL

| 期限 | offers | 24h 內成交 | 樣本下限 | 邊界涵蓋率（≥ 80%） | 邊界寬度中位數 | 有快照 | 排隊誤差中位數（≤ 60 分） | 偏差中位數 | 判定 |
|---:|---:|---:|---|---:|---:|---:|---:|---:|---|
| 2 | 38 | 32 | 達到 | **65.8%** | 0.0 分 | 37 | 0.0 分 | 0.0 分 | FAIL |

依停止條件 (a)，不進步驟 5。

## 13 張不涵蓋的分類

| 類別 | 張數 | 證據 | 性質 |
|---|---:|---|---|
| A 剔除自己成交時，連真正吃我們的借方需求也剔掉 | 4 | 實際成交早於樂觀邊界；這 4 張的成交在 tape 上都是負號（借方主動），利率等於我們的掛價；剔除後同價與更高價都沒有其他成交，replay 只能等到更晚的市場成交 | 量法：計畫的「剔除自己成交」規則讓我們是最佳 ask 時的需求消失 |
| B 我方掛單直接吃到既有 bid | 1 | 公開金額正號（出借方主動） | replay 模型缺口：沒有模擬掛價 ≤ 最佳 bid 時立刻撮合 |
| C DB 無成交，replay 數分鐘內判成交 | 6 | `funding_trades` 無紀錄；取消時間在 `ledger_observation_offer_history`，`bfx_research` 沒有權限 | 推論是被 bot 提早取消或換單（觀察窗被截斷），**未驗證** |
| D 秒級時差 | 2 | replay 比實際早幾秒成交：一張 ack 延遲 3,374 ms，replay 在 ack 前就判成交；另一張實際成交在送單後約 4 秒、replay 更早。全體 ack 延遲 p50 460 ms、p90 2,353 ms；tape 時間戳是秒級 | replay 模型缺口：掛出時間用送單開始，不是 venue 接受的時間；涵蓋判定沒有容差 |

（共 13 張。）

## 已知限制

- 只有 fUST 2 天期；其他期限桶沒有樣本。
- 排隊誤差中位數 0 分鐘，是因為多數 offer 的三條路徑同時成交（邊界寬度中位數 0）：2 天期 fUST 深度大，同價排隊很少是主要不確定性。
- 沒有保留樣本：恢復放貸前不會有新的自己成交，修正後重跑用的是同一批 38 張，屬於事後調整。
