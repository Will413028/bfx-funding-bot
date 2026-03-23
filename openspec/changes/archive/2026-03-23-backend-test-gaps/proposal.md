## Why

後端測試覆蓋率有兩個主要缺口：

1. **Handler 層只有 48.2%** — 多個 DELETE endpoint 和 edge case 未覆蓋
2. **appconfig 層 0%** — 環境變數載入、PEM 解析、AES key 驗證完全沒有測試。config 解析失敗會導致啟動就 panic，應有基本測試確保錯誤訊息清楚

## What Changes

- 新增 `appconfig/config_test.go`，覆蓋 PEM 解析錯誤、AES key 長度驗證、必要 env var 缺失
- 補齊 handler 層缺失的測試 case（DELETE 操作、edge case）

## Capabilities

### New Capabilities

- `appconfig-tests`: Config 載入的單元測試覆蓋

### Modified Capabilities

（無行為變更，純測試補齊）

## Impact

- `backend/internal/appconfig/config_test.go` — 新增
- `backend/internal/handler/*_test.go` — 補齊 test cases
