---
title: FRR auto-renew baseline 改為 sub-account 真實 A/B 量測，而非合成 benchmark 或 in-bot FRRDELTAVAR sleeve
date: 2026-09-22
status: active
tags: [bfx-funding-bot, decision, strategy, live-validation, benchmark, sub-account]
---

# FRR auto-renew baseline 改為 sub-account 真實 A/B 量測

## Context

live 量測（10 週窗、73 fills，2026-08-30）顯示 bot 落後 AlwaysFRR benchmark，cap 加碼因此不放行。但這個 benchmark 是**合成的**：[2026-07-07-e3-measurement-and-diagnostics-realm-authority](2026-07-07-e3-measurement-and-diagnostics-realm-authority.md) D2 定為 `FRR×365×(1−fee) × 市場 utilization`，用市場級 `funding_amount_used/funding_amount` 代替「自己掛 FRR 單會不會成交」；方向未知（可能高估也可能低估）。backtest 的 AlwaysFRR arm 走 linear fill，對 above-market 掛單一律判死（fill 0.19、年化 1.83%），與 live 矛盾，是被檢驗對象而非裁判。FRR 對 p2 close 的溢價在 2022 後成長到 fUST 1.7~2.2×、fUSD 2.3~3.0×（`2026-07-19-frr-floor-backtest.md` FRR unit audit），bot 只借 2 天期，結構上就在最便宜的 tenor。

prior state：[2026-07-10-strategy-review-e2-enforce-and-chunking-verdict](2026-07-10-strategy-review-e2-enforce-and-chunking-verdict.md) 把 FRRDELTAVAR sleeve 標 DEFERRED（≥8 週窗後再議）；registry 2026-07-19 把 SKIP FRR parking 歸約為「live FRR fill 量測」；[2026-05-29-credit-aware-reconcile-v2](2026-05-29-credit-aware-reconcile-v2.md) 宣告 sub-account 隔離為 next step，未做。帳戶資金屬小額 canary 規模；venue min offer 為 USD 150。

## Options Considered

- **A. 維持合成 benchmark**，等 ≥8 週窗再議（現狀）。
- **B. in-bot FRRDELTAVAR cell**：executor `build_offer_payload` 加 offer type（現硬編碼 `LIMIT`）、新 live strategy、eligibility 的 rate 來源改 venue-derived、attribution 分 cell。
- **C. sub-account auto-renew（選用）**：B 臂放 Bitfinex sub-account 開 auto-renew at FRR；主帳號跑 bot 當 A 臂；B 臂只用 read-only key 讀 credits/ledger 算同定義 APR。
- **D. 不做 live A/B**，用 backtest AlwaysFRR arm 當裁判。

## Decision

- **D1 = C**：bot vs FRR auto-renew 真實 A/B，sub-account 隔離，8 週。
- **D2 判讀規則預登記**：兩個分母（capital_days 的 realized APR、含 idle 的 budget APR）各算每週 paired diff（A − B），8 週後 bootstrap CI。CI 全在 0 之上 → bot 維持預設模式，cap 加碼 gate 的 benchmark 改用 B 臂實測；CI 全在 0 之下 → bot 對零操作使用者無附加值，產品預設改「FRR + spike overlay」、MR 擇時降為選配；跨 0 → 延長 4 週一次，仍跨 0 則結論「不劣於 FRR」。
- **D3 資金下限**：A 臂 T ≥ min offer ÷ 0.70（`cell_limit = 0.70×T` 須 ≥ min offer 150）、B 臂 ≥ min offer；合計下限約為 min offer 的 2.4 倍，入金需在此之上留餘裕。
- **D4 B 臂不進 money path**：只有 read-only loader（`scripts/report_ab_frr.py`）讀 sub-account；auto-renew 由 operator 在 venue 設定。

## Rationale

- **C 而非 A**：合成 benchmark 拿市場 utilization 代替自己 FRR 單的 fill，一個沒實測的對手在 gate 真錢加碼；A 的等待也不會讓它變真。
- **C 而非 B**：B 是 money-path 變更（offer type、eligibility、attribution），需 execution-integrity 等級 review；且同帳號的 FRR credit 會以 shared unattributed credit 進每個 cell 的 `E_cell`（I-CAP），以當時帳戶規模 A 臂 headroom 會被壓到低於 min offer 而全部 block。sub-account 零 money-path 程式碼、attribution 天然分離、順手落實 05-29 宣告的隔離。B 的 offer type 能力延後到 SKIP parking 真要上時再做。
- **不選 D**：backtest fill 模型正是本 A/B 要校準的東西，不能同時當裁判。
- **代價**：operator 手動 sub-account／transfer／key；n = 8 週、單 symbol（fUST）、小額不外推到大額資金；B 臂 period 固定 2（auto-renew 設定），與 FRR 多期限匹配不完全同構；A/B 期間 NAV 拆兩半，A 臂實際只能掛一筆。

## Expected Outcome

- 8 週後依 D2 三態之一落地，並產出 FRR 單的實測 fill／utilization。
- 副產品：book-replay fill model 的 AlwaysFRR arm 有實測值可校準（review doc 提案 C 驗收條件）；cap 加碼 gate 的 benchmark 由合成改實測。
- 成功判準：報告 `docs/research/<date>-ab-frr-baseline.md` 含兩分母 paired CI 與 honesty caveats；registry TESTING → 最終 verdict。

## Followup

- operator：確認帳號等級支援 sub-account → 建立 → halt 下手動 transfer（venue write，留操作紀錄）→ 開 auto-renew FRR、period 2、金額固定 → 建 read-only key 存於本機（不進 repo）。
- code：`scripts/report_ab_frr.py`（read-only；A 臂沿用 `attribution_weekly`，B 臂 REST `funding/credits/hist` + `ledgers`）；registry 預登記；8 週後報告落 `docs/research/`。
- 前置：恢復放貸（book WS 修復）；A 臂零成交時只累積 B 臂，不裁決。

## Revocation Triggers

- Bitfinex sub-account 不可建或內部 transfer 受限 → 退回 B（in-bot FRRDELTAVAR），重新 review money path。
- A 臂 8 週內 acknowledged fills < 20（halt 未解或 book WS 未修）→ 延長，不裁決。

## Lessons

### Rules

- **R1（benchmark 必須自己被量測）**：`Rule:` 拿來 gate 真錢決策的對照 arm 必須是真實成交量測；「模型 × 市場代理」的合成 baseline 只能標為上界或下界，不能當 gate 的對手。

## Amendment（2026-09-23）

### Amendment Decision

- D2 的第三態改寫：CI 跨 0 → **`INCONCLUSIVE`**，延長一次 4 週後仍跨 0 就以 INCONCLUSIVE 結案，**不宣稱「不劣於 FRR」**。non-inferiority margin 本輪不設定，要設定時另立 Amendment 並在 registry 預登記 margin 值。

### Amendment Rationale

- 沒有 margin 的情況下，CI 跨 0 只代表證據不足，不代表兩臂等價；原文把「沒看到差異」寫成「不劣於」是把缺乏證據當成證據（2026-09-22 strategy-correctness plan 第 5 項指出）。代價：A/B 可能以 INCONCLUSIVE 結束而沒有可執行的產品結論，那時要靠更長窗口或更大資金，而不是換判讀規則。

## Related

- 來源：本 ADR 即原始紀錄（2026-09-22 與 coding agent 討論當場拍板）。review 全文 §4：研究報告 2026-09-22-strategy-system-review（原文不在本 repo）。本 ADR 自含，來源消失仍可答「why C over A/B/D」。
- 同批裁決：[2026-06-04-adaptive-period-strategy-and-deploy-gating](2026-06-04-adaptive-period-strategy-and-deploy-gating.md) 的 2026-09-22 Amendment（AP Gate A 重設）。
- benchmark 定義：[2026-07-07-e3-measurement-and-diagnostics-realm-authority](2026-07-07-e3-measurement-and-diagnostics-realm-authority.md) D2；bot-vs-idle 主指標：[2026-05-31-g3-bot-vs-idle-reframe](2026-05-31-g3-bot-vs-idle-reframe.md)。
- FRRDELTAVAR sleeve 原裁決：[2026-07-10-strategy-review-e2-enforce-and-chunking-verdict](2026-07-10-strategy-review-e2-enforce-and-chunking-verdict.md)（本 ADR 不推翻其 DEFERRED，只把它從 A/B 前置移除）。
- sub-account 隔離的來歷：[2026-05-29-credit-aware-reconcile-v2](2026-05-29-credit-aware-reconcile-v2.md)。
- registry：strategy registry 執行層實驗表（不在本 repo）。
