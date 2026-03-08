## Why

Offer 掛出後可能只被部分成交（partial fill），剩餘金額仍留在 order book。Partial Fill Strategy 偵測長期未完全成交的 offer，建議取消重掛以改善成交率。同時評估部分成交比例，根據成交狀況調整新掛單的金額大小。

## What Changes

- 新增 `lending/strategy/partial.go`：Partial Fill 模組
  - 偵測 active offers 中金額遠小於原始掛單的（視為 partial fill 殘留）
  - 殘留量太小建議取消（低於 minBalance）
  - 根據 active offers 的整體成交比例調整新掛單金額
- 新增完整單元測試

## Capabilities

### New Capabilities
- `partial-strategy`: 偵測部分成交殘留，建議取消小額殘留，根據成交比例調整新掛單金額

### Modified Capabilities

## Impact

- 新增檔案：`lending/strategy/partial.go`, `lending/strategy/partial_test.go`
- 純計算模組，只依賴 `domain/`
