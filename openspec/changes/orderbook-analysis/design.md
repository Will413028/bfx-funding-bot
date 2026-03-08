## Context

`marketfeed/snapshot.go` 已有基礎 `computeOrderBookSummary`（最佳 bid/ask、深度統計）和 `detectWallPositions`（單一閾值巨單偵測）。但策略層需要更精密的分析：過濾 dust 雜訊、偵測分散牆、估算隱藏掛單、識別競爭者。依架構規範，`orderbook/` 是純計算 package，只依賴 `domain/`。

## Goals / Non-Goals

**Goals:**
- 建立 `lending/orderbook/` package，4 個獨立分析模組
- 遷移 `marketfeed/snapshot.go` 的 summary + wall 邏輯至 `orderbook/`
- 所有模組為純函式或輕量有狀態 struct，只依賴 `domain/`
- 提供完整測試覆蓋

**Non-Goals:**
- 不做 real-time streaming 分析（仍為 snapshot-based）
- 不做跨 symbol 分析（每個 symbol 獨立）
- 不實作 competitor counter-strategy（僅偵測，策略在 Phase D）

## Decisions

### 1. Package 結構

4 個檔案各自暴露公開函式/struct：
- `dust.go` — `FilterDust(entries, opts) []BookEntry`：回傳過濾後的 entries
- `wall.go` — `DetectWalls(entries, opts) []WallPosition`：含分散牆偵測
- `hidden.go` — `HiddenRatioEstimator` struct（需跨次計算維護歷史）
- `competitor.go` — `CompetitorDetector` struct（需追蹤掛單模式歷史）

加上 `summary.go` 將現有的 `computeOrderBookSummary` 遷移進來。

### 2. Dust Filter 策略

自適應閾值：基於當前掛單簿 median amount 的百分比（如 median × 0.05）。低於閾值的掛單視為 dust，從分析中排除但不影響原始資料。

**替代方案**：固定金額閾值 → 不同市場條件下不適用，放棄。

### 3. 分散牆偵測

在相鄰利率（rate 差距 < spread × N）有多筆中等大小掛單，聚合後總量超過閾值，視為分散牆。與單一巨單牆區分以 `WallType` 欄位標記。

### 4. Hidden Ratio 估算

比較 5 分鐘窗口的成交量與可見掛單簿深度。若成交量顯著超過可見深度，差值推估為隱藏掛單：

```
HiddenRatio = max(0, (TradeVolume - VisibleDepth) / TradeVolume)
```

需維護歷史成交量，用 struct 實作。

### 5. 競爭者偵測

追蹤掛單模式特徵：
- **跟價行為**：在 best ask 附近頻繁出現新掛單（跟蹤掛單簿快照變化）
- **Round-number 掛單**：利率為整數 bps 的比例異常高
- 輸出 `CompetitorActivity` 分數 [0,1]

### 6. Domain 型別擴充

在 `domain/snapshot.go` 新增：
- `WallType` string（"single" / "distributed"）加入 `WallPosition`
- `OrderBookAnalysis` struct 整合所有分析結果

## Risks / Trade-offs

- **[Risk] Dust filter 過度過濾** → 預設保守閾值（median × 0.05），可調參數
- **[Risk] Hidden ratio 在低成交量時不準** → 設最低成交量門檻，低於時回傳 0
- **[Trade-off] 遷移 marketfeed/snapshot.go** → 需同步更新 marketfeed 的測試，但長期架構更清晰
