## Why

Bitfinex API 有全局速率限制。多個 Worker 同時呼叫 API 可能觸發限流，導致掛單失敗。Quota Allocator 統一分配 API 呼叫配額，確保所有 Worker 在限額內運作，並根據使用者方案（plan）給予不同優先權。

## What Changes

- 新增 `lending/quota/allocator.go`：全局 API 配額分配器
  - `Allocator` struct，持有全局容量、per-user 配額 map、mutex
  - `Acquire(userID, n) bool` — 嘗試取得 n 次 API 呼叫額度，成功回傳 true
  - `Release(userID, n)` — 歸還未使用的配額（例如 batch 優化減少了呼叫次數）
  - `SetUserQuota(userID, limit)` — 設定 per-user 上限（依 plan 決定）
  - `RemoveUser(userID)` — 使用者停用時清理
  - `Remaining(userID) int` — 查詢剩餘配額
  - 全局配額使用 token bucket（定時補充），per-user 是固定 window
  - 定時 refill：每個 interval（如 1 分鐘）重置所有 user 的已使用額度
- 新增完整單元測試

## Capabilities

### New Capabilities
- `quota-allocator`: 全局 API 配額分配 + per-user 限額

### Modified Capabilities

## Impact

- 新增檔案：`lending/quota/allocator.go`, `lending/quota/allocator_test.go`
- Worker 在每次 tick 前呼叫 Acquire 確認有配額
- 未來可擴展為 Redis-based 分散式配額
