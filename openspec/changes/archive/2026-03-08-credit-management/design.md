## Context

E1 OfferExecutor 已建立掛單/撤單的執行基礎設施（FundingClient, KeyStore, Cipher, ExecutionLog interfaces）。Credit 是 offer 成交後的債權，有固定 period，到期後資金釋出。使用者若啟用 AutoRenew，系統應在 credit 即將到期時自動以類似條件重新掛單。

目前 `domain.FundingCredit` 已有 `AutoRenew bool` 欄位，`domain.ActionRenew = "renew"` 也已定義。

## Goals / Non-Goals

**Goals:**
- 掃描使用者的 active credits，識別即將到期（剩餘 ≤ 1 天）的 credits
- 對 AutoRenew=true 的到期 credit 自動提交新的 funding offer
- 續約條件：原 rate 和 period，但不低於 config 最小值
- 每次續約記錄 ExecutionRecord (action="renew")
- 回傳 RenewSummary 統計成功/失敗數

**Non-Goals:**
- 不處理 credit 的強制提前結束（Bitfinex 不支援）
- 不做 rate 優化（續約用原始 rate，進階策略由上層決定）
- 不管理 credit 生命週期狀態機（僅做到期偵測 + 續約）

## Decisions

### 1. 複用 E1 interfaces，不新增 interface

CreditManager 使用與 OfferExecutor 相同的 FundingClient、KeyStore、Cipher、ExecutionLog。這保持 execution package 的一致性，且 FundingClient.SubmitFundingOffer 已足以處理續約（續約本質就是提交新 offer）。

### 2. 到期判斷使用可注入的 now 函式

`CreditManager` 接受 `now func() time.Time` 參數，預設 `time.Now`。到期計算：`openedAt + period days - now ≤ 24h`。這讓單元測試可以控制時間，與 D10-D12 的 pattern 一致。

### 3. 續約 rate/period 取原值與 config 最小值的較大者

避免以低於使用者設定下限的條件續約。`rate = max(credit.Rate, config.Rate.Min)`、`period = max(credit.Period, config.Period.Min)`。

### 4. RenewSummary 結構與 ExecutionSummary 分開

CreditManager 回傳 `RenewSummary{RenewOK, RenewFail, Skipped int}`，語意清楚。Skipped 計算非 AutoRenew 或未到期的 credits。

## Risks / Trade-offs

- **[Race condition]** credit 到期與 auto-renew 之間可能有時間差，資金可能被其他 offer 佔用 → 由上層 worker 協調執行順序（先 credit renewal，再新 offer）
- **[Rate staleness]** 續約用原 rate 可能已不符市場 → Non-goal，進階策略由 worker 整合 market data 決定
