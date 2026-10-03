---
title: 維持 always-2d，不採「利率高於門檻才借 7/14/30 天」的 tenor 規則（early repayment 是決定因素）
date: 2026-09-27
status: active
tags: [bfx-funding-bot, decision, strategy, tenor, backtest, research]
related-commits:
  - bdb77cf
---

# 維持 always-2d，不採利率門檻的 tenor 規則

## Context

Will 問：bot 是否該停止永遠掛 2 天，改成「利率高於某門檻才借 7 天」（14／30 天一併評估）？若是，門檻多少？prior state：所有 live 策略硬編碼 `period_days=2`；AdaptivePeriod（以 deviation-from-EMA 選 2/7/14）merged 未 arm（[2026-06-04-adaptive-period-strategy-and-deploy-gating](2026-06-04-adaptive-period-strategy-and-deploy-gating.md)）；tenor 定價與 book fill 基礎建設見 [2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation](2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation.md)。既有回測引擎（`engine.py` ~L290）把每筆 credit 持有滿期，無法回答「借款人提前還款」這個核心問題，因此新寫 hourly 單位資本模擬器。

**約束**：

- `external` 沒有 p7／p14 成交序列（candles 只有 p2、p30、a30）；p30 與 fUST candles 記錄到 2026-07-19 為止；book snapshot 只有 top-25 ask、始於 2026-07-19。
- `external` 借款人可免費提前還款（Bitfinex funding credit 由借方決定何時還）。
- `external` live eligibility 需要 top-25 book 裡有 exact-period ask（[2026-08-23-funding-strategy-execution-integrity](2026-08-23-funding-strategy-execution-integrity.md) D2）。
- `inherited` live lending envelope 目前只允許 period 2–2（研究報告當日記載；envelope 機制見 [2026-09-25-lending-envelope-replaces-probation-and-account-halt](2026-09-25-lending-envelope-replaces-probation-and-account-halt.md)）；仍成立的理由：本研究是 research-only，不改 envelope。
- `external` 部署資本為小額：決定「1–2 pp」的絕對價值。

## Options Considered

- **基準. 利率門檻切長天期（lending-bot 慣例）**：rate 超過 threshold 就改掛 X 天；例：Poloniex/Bitfinex lending bot 的 `xdaythreshold`／`xdays`（[configuration docs](https://poloniexlendingbot.readthedocs.io/en/latest/configuration.html)）。本研究評了 7d 的 `r7 ≥ 0.0002/0.0003/0.0004/0.0005/0.0010 /day` 與 `r2 ≥ trailing-30d p80/p90`。
- **A（選用）. 維持 always-2d**，不加規則。
- **B. 7d whenever r7 ≥ r2**（無門檻，只要長天期不便宜）。
- **C. 30d whenever r30 ≥ r2**（吃 30d level premium）。
- **D. 7d 門檻規則當量測實驗上 live**：best visible exact-7d ask ≥ max(0.0002/day, 當前 2d rate) 才掛 7d，否則 2d。

## Decision

- **D1 = A**：維持 always-2d；不加門檻規則，live config／envelope／policy 不動。（owner-confirmed 2026-09-27：Will 拍板「先維持 always-2d，之後再研究有沒有更好的做法」。）
- **D2**：若 Will 仍要 live 試，候選只有 **D**（最佳 worst-case；trailing-30d p80 變體表現相近），且定位為「量測借款人持有行為」的實驗，不是收益變更。
- **D3**：以四種還款情境評估每條規則——held 100%／50%／25%（與價格無關的均勻提前還款）與 **refinance**（2d 市場比鎖定利率低 ≥20% 的第一個小時就還，借方的免費選擇權）；以**每月獨立模擬的 median-month Δ** 為主指標，full-window Δ 只當 fat-tail 參考。

## Rationale

- **D1 vs 基準**：7d 最佳規則 `r7 ≥ 0.0002/day（7.3% APR）且 r7 ≥ r2` 在均勻還款下 median-month +0.73～+1.07 pp（fUST 2018-12..2026-07，7d 以 r2×1.048 合成，估計值），但在 refinance 情境 −0.06～+0.03 pp；book 實測 70 天窗 +0.1～+0.4 pp。收益完全取決於未量測的借款人行為，而唯一 live 樣本（2026-09-25 兩筆 0.00019999 credit 建立 14 分鐘後即被還，當天 p2 盤中低至 0.000076–0.00016，n=2）指向 refinance 情境。以當時的小額部署資本算，上限每年僅數美元級。相較之下維持 2d 的代價只是放棄一個可能為零的 upside。
- **不選 B**：無門檻的 7d 在 refinance 下 −0.30 pp、只贏 17% 月份——有門檻的規則最差 ≈0，無門檻的 tenor 切換會賠。
- **不選 C**：30d 是唯一大的量測效果（p30 對 p2 traded premium 2024–2026 中位 +16%～+39%，r30≥r2 在均勻還款 +1.7～+2.6 pp），但它是 **level premium 而非門檻效應**（門檻 ≥0.0004 起 median 歸零），且 30d premium 在低利率月份最大、與「高利率才鎖長」方向相反；refinance 把它砍到 +0.1～+0.2 pp；exact-30d ask 只在 5–10% 小時可見、p30 序列 07-19 後停止記錄 → 現在無法執行也無法重量。
- **D3**：均勻提前還款不推翻任何結論（長天期收益按持有小時計、還回的資本以 2d 重借），所以「引擎假設持有滿期」不是灌水來源；rate-contingent 還款才是。median-month 讓少數 spike 月無法主導結果；另以前一小時 close 定價、需本小時 high ≥ 該價才成交，消除同小時 lookahead（否則 p30 0.07/day 的 rate-cap print 產生 +30 pp 假象）。代價：7d 價格是合成、refinance 門檻 20% 是判斷值、單位資本單利簡化。

## Result

- `git show --stat bdb77cf`：純研究工具——`modules/backtest/period_threshold.py`（pure simulator）、`scripts/run_period_threshold.py`（read-only DB runner，candle 或 book 來源）、12 unit tests。
- Book 可見度（fUST，hour-weighted，2026-07-19..09-27）：7d 42–61%（premium +3～+6%）、14d 5–10%、30d 5–10%；top-25 視窗幾乎被 2d 佔滿，所以 14d/30d 在 90–95% 小時沒有價格參考。外部 review 的 7 天數字（2d 100%、7d 69.5%）重現，14d/30d 差異來自取樣窗。
- 重現（`backend/`，read-only 連線到 production Postgres）：`uv run python -m scripts.audit_book_period_coverage --symbols fUST --periods 2,7,14,30 --since-ms <now-7d>`；`uv run python -m scripts.run_period_threshold --symbol fUST --start 2018-12-22 --end 2026-07-19 [--rate-cap 0.002 | --premium-bucket month]`；`--symbol fUSD --start 2016-08-01`；`--source book --start 2026-07-19`。

## Followup

- 之後研究更好的 tenor 做法（Will 2026-09-27 拍板時指定；範圍含 D 量測實驗、30d level premium、提前還款行為量測）。
- （決策）要不要跑 D 當量測實驗；需先放寬 envelope 的 period 範圍（money-path 變更，需獨立 review）。
- （fog）backfill Bitfinex `trade:1h:fUST:p7`（及 p14）candles 以取得 7d traded premium——外部下載，需 Will 核准。
- 恢復 p30 序列記錄，07-19 後的 30d premium 才能重量。

## Revocation Triggers

- live 長天期 credit 的持有時間顯示還款不隨利率下跌（median hold ≥ 50% 期限、2d 下跌後持有不變短）→ 均勻還款列適用（7d ≥0.0002 約 +1 pp、30d 約 +2 pp），重評。
- p7／p14 traded 序列顯示 7d traded premium ≳ +10% over p2 → 重評 7d。
- exact-30d ask 可見度明顯高於 10% 小時且 p30 恢復記錄 → 重量 30d premium。
- 部署資本成長到 1–2 pp 值得做。

## Lessons

### Rules

- **R1**：`Rule: 評估任何「鎖更長換更高利率」的規則，先把借方的免費提前還款建模成 rate-contingent（利率跌就還）情境；持有滿期或均勻提前還款的回測會系統性高估長天期價值。` 單一 case，未抽為通用教訓（≥2 再抽）。

## Related

- 本研究的 book 可見度數字同時是 AP Gate A0 的證據（14d exact 可見 5–10% < 50%），已記入 [2026-06-04-adaptive-period-strategy-and-deploy-gating](2026-06-04-adaptive-period-strategy-and-deploy-gating.md) Amendment。
- 來源：研究報告 2026-09-27-period-threshold（原文不在本 repo）
- 基準來源：Poloniex/Bitfinex lending bot configuration docs（`xdaythreshold`、`xdays`），URL 見 Options。
- 決策脈絡：Will 的提問與本 ADR 的裁決即原始紀錄；數字全部取自上列 research 報告。
- 相關 ADR：[2026-06-04-adaptive-period-strategy-and-deploy-gating](2026-06-04-adaptive-period-strategy-and-deploy-gating.md)（AP 以 EMA deviation 選 tenor，同一還款風險適用）、[2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation](2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation.md)、[2026-09-22-frr-baseline-ab-via-sub-account](2026-09-22-frr-baseline-ab-via-sub-account.md)
