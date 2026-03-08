## 1. Domain 層

- [x] 1.1 `domain/signal.go` 新增 `SignalIntraday SignalType = "intraday"` 常數

## 2. Signal 模組

- [x] 2.1 新增 `lending/signal/intraday.go`：Intraday struct + NewIntraday() + Name() + Compute()，含三時段偵測、30 分鐘線性過渡、低需求基線 -0.3、月內乘數、clamp [-0.5, +0.5]、confidence 1.0
- [x] 2.2 新增 `lending/signal/intraday_test.go`：測試各時段內/外、過渡區、月末/月初乘數、clamp、stateless

## 3. MDC 整合

- [x] 3.1 `lending/signal/mdc.go`：`defaultWeights` 新增 `SignalIntraday: 0.10`，移除預留註解
- [x] 3.2 `lending/signal/mdc.go`：`defaultLambda` 新增 `SignalIntraday: 0.002`
- [x] 3.3 更新 `lending/signal/mdc_test.go`：驗證 6 信號權重總和 = 1.0

## 4. 註冊 + 文件

- [x] 4.1 `cmd/server/main.go`：`newMarketFeedService` 的 sources 加入 `signal.NewIntraday()`
- [x] 4.2 `backend_architecture.md`：§3 目錄樹加回 `intraday.go`、§4.3 信號源 5→6、§5.2 信號數量 5→6
- [x] 4.3 `ROADMAP.md`：C3 描述更新為 6 信號源

## 5. 驗證

- [x] 5.1 `go test -race ./internal/lending/signal/...` 全部通過
- [x] 5.2 `go build ./...` 編譯通過
