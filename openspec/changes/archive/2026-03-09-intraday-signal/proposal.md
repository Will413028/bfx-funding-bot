## Why

MDC 權重目前只分配了 90%（5 個信號源），預留了 10% 給日內時段信號（`mdc.go:17`）。V10 策略文件 §4.5 定義了 Intraday Seasonality 信號，可根據交易時段和月內位置調整利率溢價，提升資金利用率。實作後 MDC 加權覆蓋率達到 100%。

## What Changes

- 新增 `lending/signal/intraday.go` — 日內時段效應信號模組
  - 三大高需求時段偵測（亞洲、歐洲、美國開盤）
  - 月內位置乘數（月末 ×1.1、月初 ×1.05、月中 ×1.0）
  - 信號輸出 -0.5 ~ +0.5，輸入 MDC
- 新增 `domain.SignalIntraday` 常數
- MDC 權重更新：分配預留的 10% 給 intraday 信號
- `main.go` 註冊 intraday signal source 到 marketfeed

## Capabilities

### New Capabilities
- `intraday-signal`: 日內時段效應信號計算（時段偵測 + 月內位置修正 + MDC 輸入）

### Modified Capabilities
- `market-signal`: 新增 `SignalIntraday` 常數到 SignalType enum

## Impact

- `domain/signal.go` — 新增 SignalIntraday 常數
- `lending/signal/intraday.go` — 新檔案
- `lending/signal/mdc.go` — 權重分配更新（預留 10% → intraday 10%）
- `cmd/server/main.go` — 新增 `signal.NewIntraday()` 到 signal sources
- `backend_architecture.md` — 目錄樹 + 信號源數量 5→6
- `ROADMAP.md` — 更新描述
