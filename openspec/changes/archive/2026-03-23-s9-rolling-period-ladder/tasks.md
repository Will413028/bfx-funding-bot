## 1. Domain — PeriodLadder 型別

- [x] 1.1 在 `domain/decision.go` 新增 `PeriodLadder` struct
- [x] 1.2 在 `domain/decision.go` 新增 `PeriodTierRange` struct

## 2. Strategy — PeriodLadder 計算

- [x] 2.1 在 `strategy/period.go` 新增 `ComputePeriodLadder()` 函式
- [x] 2.2 實作 tier 範圍切割
- [x] 2.3 實作退化邏輯：range < 3 或 tierCount=1 時回傳單一 period
- [x] 2.4 實作 RatePercentile 偏移
- [x] 2.5 整合 per-tier stagger（`adjustPeriodInRange` 在子範圍內碰撞避免）
- [x] 2.6 TestComputePeriodLadder_ThreeTiers — 驗證各 tier 落在正確範圍
- [x] 2.7 TestComputePeriodLadder_Degrade — range < 3 退化為單一 period
- [x] 2.8 TestComputePeriodLadder_RatePercentileShift — 高/低 percentile 偏移方向正確
- [x] 2.9 TestComputePeriodLadder_SingleTier — 1 tier 使用 ComputePeriod fallback

## 3. Composite — 整合 per-tier period

- [x] 3.1 修改 `composite.go` Stage 3：呼叫 `ComputePeriodLadder` 取得 ladder
- [x] 3.2 每個 allocation tier 的 offer 使用 `ladder.Periods[ti]`
- [x] 3.3 保留 Stage 2 的 cascade/calendar/lockup 調整作為 fallback period

## 4. 驗證

- [x] 4.1 `cd backend && go build ./...` 通過
- [x] 4.2 `cd backend && go test ./...` — 19/19 packages PASS
- [x] 4.3 ROADMAP.md S9 marked complete, 待開發 4→3
