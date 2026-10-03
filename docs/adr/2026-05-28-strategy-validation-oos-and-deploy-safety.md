---
title: 策略獲利驗證走 OOS characterization；部署只上「驗證過的固定 config」(Tier 2)
date: 2026-05-28
status: active
tags: [bfx-funding-bot, decision, backtesting, strategy-validation, deployment-safety]
related-commits:
  - "77908ac^..5269689"
  - "127f4c0^..a0cdcd3"
---

# 策略獲利驗證走 OOS characterization；部署只上驗證過的固定 config (Tier 2)

## Context

backlog #4「策略獲利量化驗證」原定 gate「月化≥1.5% AND drawdown<15%」（此 gate 由更早 session 寫進 Validation Plan，無 spec 出處）。接續驗證時發現兩前提皆破，且執行揭露 canary 部署的 MeanReversion config 實質在跑被動 FRR（零 alpha）。需決定 (a) 怎麼驗策略獲利、(b) 怎麼確保部署的是被驗證過的 config。

## Options Considered

驗證方法論：
- **A. 照字面跑絕對 gate**（1.5%/<15%）：快，但 drawdown 對放貸是 category error（equity 單調、結構恆 0）、1.5% 無錨。
- **B. OOS characterization（選用）**：yield-native 指標 + bootstrap CI + 選擇偏誤 deflated Sharpe，先攤真實數字再談門檻。
- **C. 先建放貸下檔風險模型再 gate**：最嚴謹但 pre-launch 小額 canary 規模下過度工程。

部署安全 rigor spectrum：
- **Tier 0**（現況）手挑 middle-of-grid / **Tier 1** fixed-param WFO 選擇 / **Tier 2（選用）** = Tier 1 + 部署 sanity gate + 可重現 pipeline / **Tier 3** CPCV/PBO/plateau/live-tracking-error。

## Decision

- **D1**：策略獲利驗證走 **OOS characterization**（B），不用絕對 gate；門檻錨定 baseline 實測而非拍腦袋。
- **D2**：部署安全採 **Tier 2** — fixed-param 選擇（部署驗證過的固定組）+ 部署 sanity gate（惰性/劣於 baseline config = CI fail）+ 可重現 param-derivation pipeline。Tier 3 延後 material AUM。

## Rationale

- **D1**：drawdown 是 directional 度量，放貸只賺息不虧本金 → equity 單調 → 套上去恆 0、毫無資訊；改用 active-vs-passive + worst-month + utilization + Sortino。不選 A：半個 gate 虛設、門檻無錨會誤判（baseline 實測淨 ~6% 年化，1.5% 高估 ~3×）。不選 C：放貸下檔（機會成本/平台尾部）回測本就難捕捉，平台尾部靠 cap 控不靠回測，小額 canary 規模建模型不划算。代價：OOS 數字仍 in-sample-to-selection（樂觀），真實要靠 live canary 確認。
- **D2**：root cause 不是「參數選錯一點」，而是「部署了 WFO 從沒驗過的固定組（train/serve skew）且它惰性，系統毫無察覺」。最高槓桿是 sanity gate（commit 時就攔）。不選 Tier 1（只修選擇、沒防再發）；不選 Tier 3（CPCV/PBO 對小額 canary 的邊際 rigor 不改變 sizing 決策，且 binding risk 是平台尾部、回測碰不到）。代價：要建 gate + pipeline 的一次性工。

## Result

- spec/plan/實作/結果：`git log --oneline 77908ac^..5269689`（branch `feat/oos-profitability-validation`，**未 merge**）；spec/plan 在 main `127f4c0`/`a0cdcd3`。
- OOS 跑出（Neon 2022-2026、各 49 windows）：canary MR config（ema_span=168/thr=1.0）== 被動 AlwaysFRR（active≡0、淨 ~6% 年化）；alpha 可救回（ema_span=24/thr=0.5 → active +0.06-0.07%/mo、84-86% 月份贏、對上 WFO margin 0.145/0.186）。
- D2（Tier 2）尚未實作 — 待 brainstorming→spec→plan。

## Update（Tier 2 落地）

- 已 merge main（branch 末 commit `f66e52d`）；canary 改部署 pipeline 衍生的 winner：全部 MR cell `ema_span=24, threshold_sigma=0.5`（canary fUST a30/p2 active median 0.063/0.068%/mo）。上方 Result 與 Followup 的「未 merge」「D2 尚未實作」已過時。
- `derive_cells --write/--check`（類比 `uv lock`）：EDA→grid→fixed-combo rolling OOS→選 max mean_active 且 distinguishable 的 winner；沒有 distinguishable 的就 raise（不出貨惰性 config）。`cells.yaml` 帶 `_provenance`（data_hash 釘住 committed 的 frozen candle fixture）；CI 離線跑 `--check`，不接 DB。
- `pytest -m gate`：部署中的 cell 必須贏 AlwaysFRR（IR≠0 且 pct_outperform>0，且 mean active bootstrap CI 下界 ≥0），舊的惰性 config 必須 fail。分工：`select_winner` 不跑 not_worse，由下游 CI gate 守，所以 `--write` 成功仍可能被 CI 擋。

## Followup

- **Tier 2 落地**：走 brainstorming→spec→plan 定義 fixed-param 選擇 + 部署 sanity gate + 可重現 pipeline。
- **修部署參數**：依 fixed-param 選擇結果重定 fUST（+fUSD）cells、redeploy canary；放大金額前須確認跑的是有 alpha 的 config（現實質跑被動 FRR）。
- branch `feat/oos-profitability-validation` 未 merge — 決定 merge-to-main vs 續疊。

## Lessons

### Rules
- **R1**：deploy 了 WFO 沒驗過的手選固定組且惰性而無人察覺。`Rule: 部署的參數必須出自驗證程序的輸出，不能旁邊用啟發式手挑；要部署固定 config 就對「那個固定 config」做 OOS 驗證（而非驗 adaptive policy 後部署固定組）`。

### Observations
- **O1**：WFO 用逐窗口 adaptive 選參，其 margin 是「自適應策略」成績，不會轉移到單一固定部署 config — WFO-vs-deployment 落差的通則。

## Amendment (2026-09-27): 壓縮來源補齊的裁決

補記原 spec／plan／研究中本 ADR 未載的裁決（來源見 Related）：

- **D3（OOS 方法）**：參數**固定在部署值**逐窗評估（3 月 train／1 月 test／1 月 step，2022 起約 49 窗），train 段只當 EMA warmup、不逐窗重新擬合；每窗新建 strategy instance。不選 per-window 重新最佳化：那量的是 adaptive policy，不是要部署的固定 config（即 O1）。指標省略 Calmar（MDD≡0 無定義），worst-month 取代 drawdown；bootstrap 10k draws、固定 seed；deflated Sharpe 只是 closed-form sanity check，不是 CPCV/PBO。
- **D4（gate 規則）**：cell 通過 iff (a) 可區分：`IR ≠ 0` 且與 baseline 不同的月份比例 > 0；(b) 不劣於 passive：mean active 的 bootstrap 95% CI 下界 ≥ 0。「顯著較佳」（下界 > 0）做成 flag、**預設關**——49 窗雜訊下會把真實但雜訊大的 edge 拒掉；gate 只證明「不是 passive、不比 passive 差」，真 OOS 仍是 live。
- **D5（凍結 fixture，不接 live DB）**：deploy gate 必須是 commit 的純函數。接 live DB 會讓同一 commit 依執行時間過或不過（candle 每小時追加、backfill 會改歷史）、在 CI 暴露 production credential、並把 CI 綁在 DB 可用性上；pinned、版控的資料集是資料依賴 gate 的慣例，live 比對屬監控（Tier 3）。fixture 由 derivation pipeline 產出，所以就是參數驗證所用的同一份資料。
- **D6（schema）**：cells 直接存 `ema_span:int`，刪 `ema_alpha` 與 `round(2/α−1)` 換算——換算本身是一個 skew 來源。
- **實作偏離 spec**：fixture 用 gzipped JSONL（mtime-0、sorted keys，確保 hash 決定性）而非 parquet；winner 選法由 `pick_sweep_winner`（max net_monthly）改為「固定組 rolling OOS、distinguishable 中取 max mean_active」；`--check` 只核 MR cells（RatePercentile 不部署、參數靜態），刻意縮小 spec 範圍。
- **parity（spec Risk 1）**：live marketfeed 直接 import 回測的 `MeanReversionStrategy`，無第二份實作；當時 `ema_alpha` 換算誤差約 0.037%，不會翻轉決策。所以回測驗證即部署行為。
- **研究報告現值**（2026-05-31 以 pipeline 衍生 config 重跑、改 bot-vs-idle 框架，linear fill model）：fUST a30 月中位數 0.555%（CI 0.504–0.609）、年化 7.20%、worst month 0.240%；p2 0.543%（CI 0.488–0.579）、年化 7.31%、worst 0.305%；對 AlwaysMarketRate 的 median active 0.063／0.068%/mo、贏的月份 85.7%／83.7%、IR 1.02／0.74；n_trials=9、deflated Sharpe 1.0；idle 0%、fill rate 1.0（linear 模型下退化，見 [2026-05-26-g13-candle-path-crossing-fill-model](2026-05-26-g13-candle-path-crossing-fill-model.md)）。
- **現況（2026-09-25 `b17e0c4`）**：`cells.canary.yaml` 已刪，`derive_cells --check` 與 gate 改讀 `cells.live.yaml` 作為部署子集；baseline 由 `AlwaysFRRStrategy` 改名 `AlwaysMarketRateStrategy`（`6041d1d`）。CI job `backend_gate` 仍跑 `derive_cells.py --check` 與 `pytest -m gate`。

### Revocation Triggers

- 窗數或資料來源改變到可以做 CPCV/PBO 而成本可接受，或 AUM 變成 material → 重評 Tier 3。
- live 表現系統性偏離 fixture OOS 分布 → gate 的資料假設失效，需補 live tracking-error 監控。

## Related

- 來源 spec／plan（OOS）：`2026-05-28-canary-oos-profitability-validation-design.md`、`2026-05-28-canary-oos-profitability.md`（原文已不在 repo，本 ADR 即紀錄）
- 來源 spec／plan（Tier 2）：`2026-05-28-tier2-deployment-safety-design.md`、`2026-05-28-tier2-deployment-safety.md`（原文已不在 repo，本 ADR 即紀錄）
- 研究報告 2026-05-28-canary-oos-profitability（原文不在本 repo；`.json` 見 `0936973`）。重現：`cd backend && uv run python scripts/run_oos_profitability.py`；參數敏感度實驗 `scripts/explore_mr_param_sensitivity.py`
- parity note：研究報告 2026-05-28-mr-parity-note（原文不在本 repo）
- 同日 [2026-05-28-frr-not-a-market-rate-proxy](2026-05-28-frr-not-a-market-rate-proxy.md)（FRR 解耦，解鎖策略層的前置）；bot-vs-idle 主指標見 [2026-05-31-g3-bot-vs-idle-reframe](2026-05-31-g3-bot-vs-idle-reframe.md)
