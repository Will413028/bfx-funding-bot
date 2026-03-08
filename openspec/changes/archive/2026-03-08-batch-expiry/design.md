## Context

E2 CreditManager 對每筆到期 credit 獨立續約。當使用者持有多筆同天到期的 credits，會產生大量小額 API 呼叫。Batch Expiry 作為純計算模組，負責在執行續約前將 credits 分組合併，供 CreditManager 或 worker 層使用合併後的結果。

## Goals / Non-Goals

**Goals:**
- 將到期 credits 按到期日分組（同天 = 同一 batch）
- 每個 batch 合併金額，計算加權平均 rate 和建議 period
- 若單筆 batch 超過 Amount.Max，拆分成多筆
- 純計算模組，不做 API 呼叫，不持有 FundingClient

**Non-Goals:**
- 不執行 API 呼叫（由 CreditManager 或 worker 負責）
- 不處理非 AutoRenew 的 credits（呼叫端先過濾）
- 不考慮跨天的 batch 合併

## Decisions

### 1. 純計算模組，不持有 I/O 依賴

BatchProcessor 只接受 credits + config，回傳 []CreditBatch。不像 E1/E2 需要 FundingClient。這讓 batch 邏輯可獨立測試，且呼叫端（CreditManager 或 worker）決定如何使用 batch 結果。

### 2. 到期日分組用 calendar day（UTC）

`expiresAt.Truncate(24h)` 作為 grouping key。同天到期的 credits 合併。這避免了時區複雜度（Bitfinex 用 UTC）。

### 3. 加權平均 rate

batch rate = Σ(credit.Amount × credit.Rate) / Σ(credit.Amount)。這確保大額 credit 的 rate 有更高權重，比簡單平均更合理。

### 4. Period 取 batch 內最大值

較長的 period 通常代表更高利率，取最大值避免降低已有的較長期限 credit 的潛在收益。但不超過 config.Period.Max。

### 5. Amount 超限拆分

若合併金額 > config.Amount.Max，依序填充子 batch 直到 Max，剩餘開新 batch。保持原始 credit IDs 追蹤。

## Risks / Trade-offs

- **[Rate dilution]** 加權平均 rate 可能低於某些高利率 credit 的原始 rate → 可接受，batch 的目的是減少 API 呼叫，進階策略由上層決定
- **[Batch too large]** 極端情況下大量 credits 同天到期 → Amount.Max 拆分機制處理
