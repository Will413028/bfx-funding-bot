---
title: 回測引擎摩擦模型——集中式 BacktestConfig、預設即保守真實值、EV 決定式 fill、gap 併入 cooldown
date: 2026-05-10
status: active
tags: [bfx-funding-bot, decision, backtest, fill-model, spec-compression]
related-commits:
  - "18c23e3^..ca8e611"
---

# 回測引擎摩擦模型（v2 Phase 1）

## Context

Checkpoint 2 的 0.4028%/月出自一個 toy 引擎：所有掛單立即成交、沒有手續費、沒有 credit 到期後的閒置空窗（[2026-05-09-python-rewrite-and-day3-stack](2026-05-09-python-rewrite-and-day3-stack.md)）。在這種引擎上比較策略，A/B 數字沒有意義，所以 v2 的三條工作線定為「引擎 → 資料 → 策略」，引擎先做。本 ADR 是那次引擎升級的設計取捨；其中 fill 機率模型後來被 empirical artifact 取代，取代的部分標在 Decision。

**約束**：

- `external` Bitfinex 從放貸利息抽 15% 平台費：venue 規則，至今仍成立。
- `external` 當時只有 1h candle，沒有 order book 歷史；queue position 模型要 WS 資料才做得出來。
- `inherited` 當時的 FRR 資料尚未回填（Phase 2 才做），所以市場利率先用 candle close 代替：決策當時成立；之後 FRR 被判定不是市場利率，candle close 反而成了唯一來源（[2026-05-28-frr-not-a-market-rate-proxy](2026-05-28-frr-not-a-market-rate-proxy.md)）。

## Options Considered

- **基準. 事件驅動、模擬 order book 排隊位置的 fill**（例如 [hftbacktest](https://hftbacktest.readthedocs.io/)）：精度最高，但需要 book 與逐筆成交歷史；spec 把它列為 v3+。
- **A. 摩擦參數以 kwargs 分散傳入**／**B. 集中在 frozen `BacktestConfig`（選用）**。
- **C. Monte Carlo 抽樣決定是否成交**／**D. EV 決定式（選用）**：`equity *= 1 + rate × fill_prob × period`，其中 `fill_prob = max(0, 1 − α·spread)`、α=5。
- **E. 把 gap 從有效放貸天數裡扣掉**／**F. 把 gap 加進 cooldown（選用）**：`ceil(gap_minutes/60)` 根 candle。
- **G. 提供 `no_friction()` preset**／**H. 預設值即保守的真實值、不提供 preset（選用）**。

## Decision

- **D1 = B**：`run_backtest(candles, strategy, config=None)`，`None` 時使用預設的 `BacktestConfig()`；結果分開輸出 `gross_monthly_return_pct`、`net_monthly_return_pct`、`fill_rate`。
- **D2 = D**：EV 決定式 fill，市場利率取 candle close；取不到市場利率時視為立即成交、不 raise。**已被取代**：[2026-08-23-funding-strategy-execution-integrity](2026-08-23-funding-strategy-execution-integrity.md) D3 規定預設為 empirical artifact，linear 只能在明示的離線指令下以 `fill_model="linear-baseline"` 使用；book-replay 證據見 [2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation](2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation.md)。
- **D3 = F**：gap 預設 30 分鐘（journal 估約 20 分鐘，取保守值），只拉長 cooldown，不扣有效天數。
- **D4 = H**：預設 `fee_rate=0.15`、`gap_minutes=30`、`fill_alpha=5`；想要零摩擦就自己建構 config。

## Rationale

- **D1**：Phase 3 要比較多個策略，必須共用同一份摩擦設定，集中管理才能保證一致；frozen dataclass 會在 `__post_init__` 擋掉越界值。不選 kwargs：每個呼叫點都可能各自漏傳一個參數。
- **D2**：決定式的結果可重現，測試可以寫死期望值（constant rate 從 0.3 改成 0.255，差的就是 15% 手續費）。不選 Monte Carlo：要多次抽樣才能收斂，也無法做單點斷言。不選 book 模擬：當時沒有資料。代價：spread 大於 0 的策略會被低估（fill_prob<1 時 cooldown 照樣計入），而 spread=0 時 fill 恆為 1，這個退化後來造成循環論證（[2026-07-06-profit-review-execution-layer-first](2026-07-06-profit-review-execution-layer-first.md) R1），也是 D2 被取代的原因。
- **D3**：equity 在放貸期間照常累積，gap 透過減少週期數自然扣掉收益，相較 E 不會重複計算。代價：`gap_candles` 假設 1h candle，換 timeframe 時會算錯。
- **D4**：預設值如果是零摩擦，很容易忘了開，引用出去的數字就會偏樂觀。無摩擦的 v1 數字已經記在 Checkpoint 2，不需要為它保留 preset。

## Result

- `git log --oneline 18c23e3^..ca8e611` 是實作區間（config、引擎、CLI 三個 commit）。
- 30 天真實資料重跑（`ca8e611`）：717 根 candle、15 筆、fill_rate 1.0、gross 0.3666%/月、net 0.3115%/月、drawdown 0%。之後比較策略都以 net 為基準。
- D1、D3、D4 在 origin/main 的 `modules/backtest/config.py`／`engine.py` 仍然有效；D2 的 linear 公式保留為 `compute_fill_prob`，只作為 sensitivity 基準。
- **O1**：spec 在 Risks 裡列出的「gap=15/30/60 敏感度 sweep」查不到執行紀錄。30 分鐘至今仍是猜測值。

## Revocation Triggers

- live fill 量測顯示實際的 credit 到期到下一筆成交間隔明顯偏離 30 分鐘 → 以量測值重設 `gap_minutes`。
- 回測開始使用非 1h 的 candle → 先把 `gap_candles` 改成依 `candle_minutes` 換算。
- Bitfinex 調整平台費率 → 更新 `fee_rate` 預設值，並重跑受影響的基準數字。

## Related

- 來源（Provenance）：
  - `2026-05-10-engine-upgrade-design.md`（原文已不在 repo，本 ADR 即紀錄）
  - `2026-05-10-engine-upgrade.md`（原文已不在 repo，本 ADR 即紀錄）
- 相關 ADR：[2026-05-09-python-rewrite-and-day3-stack](2026-05-09-python-rewrite-and-day3-stack.md)、[2026-05-28-frr-not-a-market-rate-proxy](2026-05-28-frr-not-a-market-rate-proxy.md)、[2026-08-23-funding-strategy-execution-integrity](2026-08-23-funding-strategy-execution-integrity.md)、[2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation](2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation.md)。
