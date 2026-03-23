## 1. Worker 狀態新增

- [x] 1.1 在 `LendingWorker` struct 新增 `trancheIndex int` 欄位
- [x] 1.2 定義 `trancheRatios = [3]float64{1.0/3.0, 1.0/2.0, 1.0}` 常數
- [x] 1.3 定義 `minLadderingBalance = 100.0` 常數

## 2. tick 邏輯修改

- [x] 2.1 在 Phase 2 與 Phase 3 之間插入 laddering 限縮邏輯
- [x] 2.2 實作限縮：`ctx.Available *= trancheRatios[trancheIndex]`（僅 Available >= minLadderingBalance）
- [x] 2.3 Phase 4 成功部署後遞增 trancheIndex
- [x] 2.4 trancheIndex >= 3 時 reset 為 0

## 3. Reset 條件

- [x] 3.1 ReloadConfig() 中 reset trancheIndex = 0

## 4. 測試

- [x] 4.1 TestWorker_TemporalLaddering_ThreeTranches — 三次 tick 各看到正確的 Available
- [x] 4.2 TestWorker_TemporalLaddering_SmallBalanceSkip — Available < 100 不限縮
- [x] 4.3 TestWorker_TemporalLaddering_ResetOnConfigChange — ReloadConfig reset
- [x] 4.4 TestWorker_TemporalLaddering_NoIncrementOnEmptyDecision — 空 decision 不遞增

## 5. 驗證

- [x] 5.1 `cd backend && go build ./...` 通過
- [x] 5.2 `cd backend && go test ./...` — 19/19 packages PASS
- [x] 5.3 ROADMAP.md M7 marked complete, 待開發 3→2
