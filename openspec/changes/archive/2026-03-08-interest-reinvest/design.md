## Context

放貸利息每日入帳到 funding wallet，成為可用餘額的一部分。目前 worker 主循環只處理策略決策產生的新 offer，不會主動偵測「多出來的」可用餘額。Interest Reinvest 作為執行層的補充模組，在每次 worker tick 時檢查是否有閒置資金可部署。

## Goals / Non-Goals

**Goals:**
- 當可用餘額（扣除已掛單和策略預留後的淨閒置）≥ Amount.Min 時，提交再投資 offer
- 使用保守參數（Rate.Min + Period.Min），避免過度風險
- 記錄 ExecutionRecord 以追蹤再投資行為
- 回傳 ReinvestSummary 統計結果

**Non-Goals:**
- 不追蹤個別利息入帳事件（由 Bitfinex 端管理）
- 不做智慧定價（保守參數即可，進階策略由上層決定是否覆蓋）
- 不區分利息收入和其他來源的可用餘額（統一視為閒置資金）

## Decisions

### 1. 接受 available 參數，不自行查詢餘額

呼叫端（worker）已有最新的 funding wallet 資訊，直接傳入 available 金額。避免 InterestCollector 自行呼叫 GetFundingBalance，減少 API 消耗。

### 2. 保守參數策略

再投資用 Rate.Min + Period.Min — 這是最低風險的配置。如果市場條件好，worker 的主策略循環會在下次 tick 產生更好的 offer。再投資的目的是「不讓資金閒置」而非「最佳化收益」。

### 3. 單筆 offer，不拆分

再投資金額 = min(available, Amount.Max)，產生一筆 offer。不像 E3 BatchCredits 需要拆分，因為這裡的金額通常較小（利息累積），單筆即可。

## Risks / Trade-offs

- **[Double deployment]** Worker 主策略可能同時要部署同一筆餘額 → 由 worker 協調：先跑策略，再用剩餘 available 跑 reinvest
- **[Frequent small offers]** 每次 tick 都可能產生小額 offer → Amount.Min 門檻防止碎片化
