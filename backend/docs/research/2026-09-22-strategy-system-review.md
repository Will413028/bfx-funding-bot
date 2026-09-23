# 2026-09-22 Strategy System Review — 收益槓桿盤點、gate 重設、bot-vs-FRR A/B 計畫

> 性質：單 session design review（非 07-06 的 43-agent 版本）。證據全部引 repo 內報告、wiki 專案頁與程式碼；
> live 數字截至 2026-08-30 G3 報告，本次未直連 VM 資料庫。
> 本文**不是 ADR**：§3–§7 是待 Will 裁決的提案，裁決後各自立 ADR（second-brain `decisions/`）並同步 registry。
> 上一份 source of truth：`2026-07-06-profit-design-review.md`（E1–E3 已 LIVE；本文接續其 §2 P1 / §3 P2 未完項）。

## 0. TL;DR

1. 正確性骨幹遠超此規模需要；策略層自 07-27 起零變動，且 live 大部分時間 halted。**停機期間任何策略工作的收益貢獻為零**，恢復放貸（wiki Pending 🔴 book WS）排第一。
2. **結構性問題**：bot 只借 2 天期，但 FRR 對 p2 close 的溢價在 2022 後成長到 1.7~3×（§1 表）。2 天期擇時 alpha backtest +0.85~1.03%/yr、live ≈ 0；再好的 2 天期擇時也贏不了 FRR auto-renew。收益槓桿在**期限結構與成交**，不在利率方向預測。
3. **Backtest 分不出關鍵槓桿**：linear fill 模型在 quote=close 時 fill 恆為 1，所有部署策略都 quote close；AlwaysFRR arm 在 backtest 只有 fill 19%、live 卻贏 bot，矛盾即模型錯。book 快照錄了兩個月沒人讀、G13 沒接、`optimizer_live` 永遠拿不到 artifact。
4. **收益側工作全部卡在需要 live 窗口的 gate 後**（AP Gate A、FRR sleeve、cap 加碼、ladder enforce），而 gate 假設連續交易，兩個月未成立。
5. 提案（§3–§7）：A) AP gate 重設；B) bot vs FRR auto-renew 真實 A/B（資訊密度最高）；C) book-replay fill model；D) period-aware 四臂 backtest；E) 每週自動重驗 job。§8 否決清單；§9 執行順序。

## 1. 現況數字

| 項目 | 數字 | 來源 |
|---|---|---|
| backtest bot-vs-idle 年化 fUST / fUSD | 7.70% / 10.3–10.7%（fill=1.0） | `docs/research/2026-06-02-oos-full-history-profitability.md` |
| L4 v2 candle 失真折扣（1× / 2×） | 月中位數 −3~−7% / −8~−12% | `2026-07-27-l4v2-distortion-sensitivity.md`、`-2x.md` |
| MR 擇時 alpha backtest / live | +0.85~1.03%/yr / ≈0 | 同上 / G3 |
| live bot vs AlwaysFRR（util-adjusted） | −1.63pp，10 週窗、73 fills | wiki 專案頁 2026-08-30 |
| FRR×365 對 p2 close 的倍數（2022–2026 年中位數） | fUST 1.7~2.2×、fUSD 2.3~3.0× | `2026-07-19-frr-floor-backtest.md` FRR unit audit 表（close/(frr×365) = 0.46~0.59 / 0.33~0.43） |
| 訊號假設 | 8 測、7 KILL、1 NOT-PROMOTED | wiki `strategy-registry.md` |
| NAV | ~$391 | wiki 專案頁 |
| live cells | fUST a30 / p2，MR ema24 thr0.5，period 固定 2 | `configs/cells.live.yaml` |

規模備註：NAV $391 × 10% = $39/yr。這個帳戶的價值在驗證 SaaS 命題，不在美元；因此 §3–§7 以**資訊價值**排序。

## 2. Findings

### F1 訊號軸已被自己的漏斗證偽（CONFIRMED）

- 證據：registry 信號表 8 個 operationalization，7 KILL、1 NOT-PROMOTED（`spike_z_extH`，re-open 條件未滿足）；G3 MR alpha ≈ 0。
- 影響：forward-rate 預測這條線再投入的期望值接近零。
- 狀態：`/strategy-research` 命令本身已寫「主要輸出會是 KILL」。

### F2 期限軸從未被正確測過（CONFIRMED）

- 證據：`strategies/mean_reversion.py` `decide()` 硬編碼 `period_days=2`；`rate_percentile.py` 同；只有 `adaptive_period.py` 動 period。
- a30 tenor 錯配：a30 cell 用 2–30 天聚合序列的 close 定價卻掛 2 天單；backtest 引擎 `engine.py` 以 `gross_equity *= 1 + gross_rate × period` 直接拿 a30 rate 乘 2 天，**a30 cell 的 backtest 數字被高估**（07-06 review §2 已列為 P1「a30 tenor 錯配 telemetry」，未做）。
- FRR 溢價成長（§1 表）指向 term premium 是 2022 後市場的主要結構；bot 完全不在那個 tenor。
- AP 是唯一動 period 的策略，backtest 結論「period 是唯一 alpha lever」（`adaptive_period.py` docstring），merged UNDEPLOYED，卡 Gate A（見 F5）。

### F3 Fill 模型退化，backtest 分不出執行層差異（CONFIRMED）

- 證據：`modules/backtest/config.py` `compute_fill_prob`：spread ≤ 0 → 1.0，否則 `1 − 5 × spread`。`scripts/run_oos_profitability.py:61` `RESEARCH_CONFIG = BacktestConfig(fill_model="linear-baseline")`；`matrix.py` 接受 `fill_model` 參數但 runner 從未載入任何 artifact。
- 所有部署策略 quote = `candle.close` → spread = 0 → fill ≡ 1.0。任何執行層改動（E1/E2、ladder、optimizer）在 backtest 裡不可分辨。07-06 review §0 的「循環論證警告」至今未解。
- 矛盾證據：同一模型下 AlwaysFRR arm 年化 1.83%、fill 0.19（`2026-07-19-frr-floor-backtest.md`），live 卻是 bot 輸 AlwaysFRR。模型把 p2 close 當多期限 FRR 單的市場參考價，對 above-market 掛單一律判死。
- 閒置資產：`funding_book_snapshots` 自 2026-07-19 起 hourly × 2 symbols（`deploy/vm/live.env` `BFX_BOOK_SNAPSHOT_ENABLED=true`、`book_snapshot.py` `DEFAULT_INTERVAL_S=3600`），無任何讀者；G13 `FillRateLearner` 寫好未接 matrix runner（ROADMAP 自認）；`rate_optimizer.py` score = rate × fill_prob × (1−fee) 已寫好，但 `optimizer_live` 要求 empirical artifact，artifact 不存在 → 永不啟用。

### F4 AlwaysFRR benchmark 是合成的，未實測（CONFIRMED）

- 證據：`weekly_attribution.py` docstring：`baseline_frr_util_apr_net_pct = FRR×365×(1−fee) × mean(市場 utilization)`。這是「FRR 單只在 market ≥ FRR 時成交」的市場級近似，不是實際掛 FRR 單的 fill。
- registry 已把 SKIP FRR parking「歸約為 live FRR fill 量測」，FRRDELTAVAR sleeve 標 DEFERRED（≥8 週窗後再議）。
- 影響：cap 加碼 gate（AlwaysFRR spread 非負）綁在一個沒實測過的對手上；−1.63pp 可能高估也可能低估。

### F5 收益側工作全部排在需要 live 窗口的 gate 後（CONFIRMED）

- AP Gate A = live MR G3 ≥8 週窗 favourable（ADR 2026-06-04 D4）；FRRDELTAVAR sleeve DEFERRED 到 ≥8 週窗；cap 加碼 = G3 PASS + FRR spread ≥ 0；ladder enforce = observe 數據證明 uplift（observe 需 live 送單）。
- live 時間軸：07-27 halt（candle bug）→ 08-05 新帳號、trading stopped → 08-30 短窗 bounded soak → 09-21 halt id=7，canary 送單全被 `book_stale` 擋。週窗自 08-30 後幾乎沒有新增。
- ADR 2026-06-04 給 Gate A 的兩個理由：(a) 先用真錢證明 live 執行路徑；(b) 用 live-fill learnings 校準 B1 tolerance。兩者都是「執行路徑被證明」，不是「MR alpha 被證明」。執行路徑自那之後已由 E1/E2 live enforce、G3 73 fills、PR #11–#15 money path 完成獨立證明；Gate A 的目的已被其他證據滿足，但 gate 文字沒有更新。
- 近兩個月 commit 主題全在 DR / release identity / capital authority，策略層零變動。

### F6 AP 的 mid/long tier 可能被 exact-period eligibility 擋死（PLAUSIBLE，需 A0 查證）

- 證據：ARCHITECTURE §4 7c「存在 exact period level，且該 side 絕對 depth 足以覆蓋 amount」；`period_pricing.py` 以 `candidate.offer_duration_days` 取 exact-period snapshot。
- AP 的 p_mid=7 / p_long=14 offer 需要 book 上有 period 7 / 14 的 ask level。funding book 多數 ask 在 period 2 與 30/120；7 與 14 是否常態存在未知。若罕見，AP 在 `book_guarded` 下會持續 `BlockedExecution`，Gate B parity 再綠也送不出單。
- 這正是 ADR 06-04 「edge 又 fill-fragile」的具體機制，兩個月的 `funding_book_snapshots` payload（每 level 含 period）可以直接回答，零風險。

## 3. 提案 A — AdaptivePeriod gate 重設

**現行**：Gate A（live MR G3 ≥8 週窗 favourable）∧ Gate B（AP shadow/parity）∧ human sign-off ∧ manual deploy。

**問題**：Gate A 把 AP arming 綁在 MR 的 live 驗證上，而 live 驗證需要連續交易；F5 說明此假設兩個月未成立，且 Gate A 的原始目的已被其他證據滿足。

**提案**：

- Gate A 改為「執行路徑已證明」的**可累積、不依賴 MR 的**證據：
  1. 現行 money path（PR #15 之後）下 ≥ 20 筆 acknowledged live fills，且無未解 UNKNOWN；
  2. 新帳號至少一份 G3 報告成功產出（不要求 verdict PASS，只要求量測鏈可用）。
- Gate B 不變，但寫成可驗收的數字：`shadow-p14` profile（`deploy/vm/shadow-p14.env` + `configs/cells.experimental-p14.yaml`）連續 7 天 `divergence=0`、errors=0。**shadow 不依賴 live，今天就能起跑。**
- 新增 **Gate A0**（本提案的前置，見 F6）：從 `funding_book_snapshots` 統計 2026-07-19 起每個 snapshot 是否存在 period ∈ {7, 14, 30} 的 ask level、該 level 的 depth 分佈。若 period 7/14 覆蓋率 < 50%，AP 參數必須重議（p_long 改 30、或 eligibility 改「period ≤ 某 level 的 period」規則，後者是 money-path 變更需獨立 review）。A0 是一支 read-only SQL，半天內可完成。
- Arming 方式：走 Halt 2 bounded canary 機制（一 account、一 symbol、一 cell、一道 command、`canary_command_permit`），cell = fUST a30 p14，金額 = 一筆 minimum offer。

**風險**：AP 的 14 天鎖定放大 venue tail；以 bounded canary 一筆 minimum offer 起步，tail 上界 = 一筆 min offer × 14 天。

**驗收**：A0 報告落 `docs/research/`；Gate B 7 天報告；bounded canary 兩個完整 reconcile cycle 後的 attribution 行。

**待裁決**：是否接受以「執行路徑證據」取代「MR alpha 證據」作為 Gate A。裁決後立 ADR，並更新 ADR 2026-06-04 的 review note 與 registry AP 列的 gate 欄。

## 4. 提案 B — bot vs FRR auto-renew 真實 A/B

**目標**：用真實成交回答四個問題：(1) AlwaysFRR benchmark 是否成立；(2) FRR 單的實際 fill 與 utilization；(3) SKIP 時該不該 park 在 FRR；(4) 產品對零操作使用者有沒有附加值。這是 SaaS 命題的直接檢驗。

**設計**：

| 臂 | 內容 | 資金 |
|---|---|---|
| A（bot） | 現行 live profile，MR fUST cells，`book_guarded` + E1/E2 | ≥ $215 |
| B（baseline） | Bitfinex auto-renew at FRR，period 2，金額固定 | ≥ $150 |

資金下限推導：min offer = USD 150；A 臂 `cell_limit = 0.70 × T` 必須 ≥ 150 → T ≥ $215（低於此每張單都被 I-CC 擋）。B 臂一筆 min offer。合計 ≥ $365，現有 NAV $391 剛好夠，無餘裕；建議入金到 ≥ $450 讓 A 臂能掛一筆 150 加上非 dust 餘額。

**隔離方式（兩個選項，建議 1）**：

1. **Bitfinex sub-account**：B 臂放在 sub-account，UI 開 auto-renew（rate 0 = FRR）。零 money-path 程式碼；attribution 天然分離；不觸碰 I-VTA/I-CAP（若放同一帳號，B 臂的 credit 會以 shared unattributed credit 被算進每個 cell 的 E_cell，在 $391 規模下直接把 A 臂 headroom 壓到 < 150 而全部 block）。需要：sub-account 建立（確認帳號等級支援）、內部 transfer（venue write，須在 halt 下由 operator 手動執行並記錄）、B 臂 read-only API key（存 vault/secrets，回報 `done`）。這也順手推進 Pending 的「sub-account 隔離」項。
2. **In-bot FRRDELTAVAR cell**：executor `build_offer_payload` 加 `type` 參數（現在硬編碼 `"LIMIT"`，`live_executor.py:91`）、新 `always_frr` live strategy、eligibility 對 FRR 型 offer 的 rate 來源改為 venue-derived。這是 money-path 變更，需 execution-integrity 等級 review。長期產品功能（SKIP parking、degraded-mode 保險）會需要它，但**不是 A/B 的前置**。
   - 注意：repo `docs/bitfinex-api-v2.md` 只記 LIMIT；FRRDELTAVAR / FRRDELTAFIX 語意需先補文件並實測 submit response 的 OFFER_TYPE 欄位。

**量測**：

- 新 loader `scripts/report_ab_frr.py`（read-only）：A 臂沿用 `attribution_weekly`；B 臂用 sub-account REST `funding/credits/hist` + `ledgers` 算同一定義的 `realized_apr_net_pct`（capital_days 分母）與 budget-level APR（含 idle）。
- 指標：每週 paired difference（A − B）on 兩個分母；8 週後 bootstrap CI。
- 判讀（預登記，跑前寫進 registry）：
  - CI 全在 0 之上 → 保留 bot 為預設模式；cap 加碼 gate 的 FRR benchmark 改用 B 臂實測值。
  - CI 全在 0 之下 → bot 對零操作使用者無附加值；產品預設模式改為「FRR + spike overlay」（ladder enforce 路徑），MR 擇時降級為選配。
  - 跨 0 → 延長 4 週一次；仍跨 0 則結案 **INCONCLUSIVE**（2026-09-23 修正：沒有 non-inferiority margin 不得宣稱「不劣於 FRR」；margin 另議並預登記）。
- 停損：任一臂出現未解 UNKNOWN 或 orphan 以外的 reconcile 異常，暫停實驗（不影響 halt 語意）。

**Honesty caveats**：n = 8 週；單一 symbol（fUST）；金額極小無 market impact，結論不外推到 10k 規模；regime 單一。

## 5. 提案 C — book-replay fill model

**資料**：`funding_book_snapshots`（2026-07-19 起，hourly，top-25 levels，每 level `[rate, period, count, amount]`）+ `funding_candles.volume`（每小時成交量）+ live fills（`event_log` submit→fill latency，`report_execution_quality.py` 已能算）。

**模型**（queue-position replay）：對時刻 t 的假想 offer (r, p, amount)：

- `queue_ahead(t) = Σ amount of asks with period == p and rate ≤ r`（同價 FIFO 全部視為在前）；
- 之後 H 小時的 `Σ volume` ≥ queue_ahead + amount → 視為 H 內成交；
- 對 spread bucket（沿用 G13 `BUCKET_GRID_BPS`）× horizon（1/4/24h）× period 聚合出 fill_prob 與 ttf 分佈，產出 `FillModelArtifact`。
- 校準：用 live fills 的實際 latency 對照模型預測（雙 bound：模型 vs 實測），這是方法論頁 §8 要求的 bound 夾擊。

**契約變更**：`_preflight_empirical_model` 與 eligibility 目前要求 `artifact.source == "candle"`；新增 `source == "book"` 需擴充 `FillModelArtifact` scope 規則（tech 頁 Case 11：artifact 不可跨 scope 重用，book 與 candle 是不同 scope，不得混用）。

**接線**：`run_oos_profitability.py` / `matrix.py` 加 `--fill-model book`；runner 對「所有 arm mean fill == 1.0」的結果 warn 並在報告標 degenerate（07-06 §2 P1 項）。

**立即可做的一行**：`deploy/vm/live.env` 加 `BFX_BOOK_SNAPSHOT_INTERVAL_S=300`（一次 REST call，儲存 ≈ 600 MB/yr）。5 分鐘粒度才對得上 65 分鐘 TTL 與 E1 30 分鐘 min-age。

**驗收**：artifact 產出且 sample_count ≥ MIN_SAMPLES 於 spread ∈ [0, +200bps]；AlwaysFRR arm 在 book 模型下的 fill 與提案 B 的 B 臂實測同量級（否則模型或 benchmark 其一有錯，兩者互為校準）。

## 6. 提案 D — period-aware 四臂 backtest

**目標**：正確評估期限結構這個唯一沒測過的軸，並修 a30 tenor 錯配。

**引擎變更**：`run_backtest` 接 `series_by_period: Mapping[int, list[FundingCandle]]`，`_apply_friction` 依 `decision.period_days` 選市場序列：2 → p2、30 → p30、其他 → a30（並在報告標記「以 a30 近似」）。策略 observe 的序列與定價序列分離（沿用 L4 v2 已引入的 `market_candles` 機制）。

**四臂（預登記）**：Always2d(p2)、Always30d(p30)、AP(2/7/14)、AlwaysFRR（book fill）；對照 MR deployed。fUST p30 sparse → 沿用 Phase 4.3 LOCF staleness budget。DSR trials：registry strategy-layer 34 → 38。

**判讀**：若 Always30d 扣費扣 fill 後仍 ≫ Always2d，產品命題由「擇時」轉為「期限」，AP 的 p_long 應重掃到 30。若不是，term premium 主要是 fill 幻覺，回到提案 B 的實測。

## 7. 提案 E — weekly 自動重驗 job

擴充 `docker-compose.bot.yml` 的 `weekly-report`（`bfx-weekly-report.timer`，Mon 04:17 UTC）。**營運報告在前、研究步驟在後，各步驟獨立 exit code，研究失敗不得阻斷 G3**：

1. `ingest_funding_stats` → `run_weekly_attribution` → `run_g3_live_validation`（現有）
2. `ingest_perp_funding` / `ingest_liquidations` topup（腳本已在，Pending 標「無排程」）
3. `learn_fill_rate --source book`（提案 C 產物，artifact 版本化）
4. `run_oos_profitability` 對 registry 中 DEPLOYED 與 NOT-PROMOTED 各列重跑，窗口隨資料延長；`--n-trials` 讀 registry 累計值
5. `diff_research_report.py`（新，小）：與上週 JSON 比較，輸出 champion 漂移與 challenger 反超
6. 全部落 `~/bfx/reports/<date>-weekly-research.{md,json}`

**規則（預登記）**：

- champion 漂移：deployed 策略 rolling 12 個月 median monthly 跌破其全歷史 p25 → 報告標紅，人工 review。
- challenger 反超：NOT-PROMOTED 候選連續 3 週 paired diff CI 全在 0 之上 → 觸發 registry 的 re-open 條件檢查。
- **永不 auto-promote**（`/strategy-research` 紅線 2）。

成本：L4 記錄單輪 4 cells 約 9 秒；全 registry 重跑 < 5 分鐘。研究步驟跑在一次性容器，不碰 live bot（沿用 `/strategy-research` 的 VM 執行模式）。

## 8. 否決 / 不做

- forward-rate 訊號研究：停。registry do-not-repeat 規則已足夠擋重測；新假設必須是期限、成交、幣種配置三軸之一。
- 重建 Go-era 13 模組 pipeline：不。07-06 review 已證明槓桿在執行層與期限，不在溢價模組堆疊。
- Hidden offers、cap 加碼、FRRDELTAVAR 作為 A/B 前置：維持 registry 現有裁決。
- 把 backtest fill=1.0 的年化數字當產品文案：不，L4 v2 已下修 8~12%，且 fill 未實測。

## 9. 執行順序與相依

| 序 | 項目 | 相依 | 規模 | 風險 |
|---|---|---|---|---|
| 0 | 解 book WS，恢復放貸 | wiki Pending 🔴 | 既有項 | 既有 |
| 1 | A0：snapshot period 覆蓋率查詢 | 無 | 半天 | 零（read-only） |
| 2 | C-0：snapshot cadence 300s | 無 | 一行 env + 一次 deploy | 零 |
| 3 | B：sub-account A/B 起跑 | 0；入金 ≥ $450 建議 | 1 天 setup + 8 週等待 | 低（venue transfer 為 operator 手動） |
| 4 | D：period-aware 引擎 + 四臂 | 無（用既有資料） | 2–3 天 + review | 零 live 風險 |
| 5 | C：book-replay artifact + 接線 | 2 累積 ≥ 2 週；或先用 hourly | 2–3 天 + review | 零 live 風險 |
| 6 | E：weekly job 組裝 | 4、5 的腳本 | 1 天 | 低（一次性容器） |
| 7 | A：gate 重設 ADR → shadow-p14 7 天 → bounded canary | 1 的結果、0 | ADR 半天；shadow 7 天 | 低（bounded） |

3、4、5 互不衝突可平行；0 是所有 live 量測的前提。

## 10. 裁決與同步狀態（2026-09-22）

Will 接受 §3 與 §4（sub-account 方案）。已同步：

- ADR：`wiki/projects/bfx-funding-bot/decisions/2026-06-04-adaptive-period-strategy-and-deploy-gating.md` 加 2026-09-22 Amendment（Gate A 替換 + A0 + Gate B 數字化）；新 ADR `decisions/2026-09-22-frr-baseline-ab-via-sub-account.md`（A/B 設計、資金下限、判讀規則預登記）。
- registry：AP 列 gate 欄改指 Amendment；執行層表加「bot vs FRR auto-renew A/B」TESTING 預登記列；FRRDELTAVAR 列註明不作 A/B 前置；SKIP FRR parking 列改由 B 臂裁決。
- wiki Pending：AP B3 項改新三 gate；cap 加碼項加 benchmark 待實測 sub-bullet；新增 A/B 項；sub-account 隔離項連到 A/B。
- 未裁決、未預登記：§5 C（book-replay fill model）、§6 D（四臂 period-aware backtest）、§7 E（weekly job）。§6 的 DSR 34 → 38 在 D 開跑前才計入。
