## Context

G0 composite pipeline 已上線，所有 13 個策略模組通過 exported helper 函數串連。P1 的 8 項改動都在現有 helper 函數或 composite pipeline 內部修改，不需新增 interface 或改動 worker 主循環。

## Goals / Non-Goals

**Goals:**
- 8 項 P1 改動全部實作，每項獨立可驗證
- 不改動 worker.Strategy interface、DecisionContext、DecisionResult

**Non-Goals:**
- 不做 P2+ 的項目（FRR 趨勢、智慧貼牆等）
- 不做 DB schema 變更

## Decisions

### D1: S2 Auto-Renew — 在 credit executor 層反轉

目前 `execution/credit.go` 的 `RenewCredit()` 預設不 renew。改為：
- 新增常數 `autoRenewDefault = true`
- `ProcessExpiring()` 改為：正常情況下走 pricing pipeline 重新定價，只有在 pipeline 失敗時才 fallback 到 auto-renew（此時 auto-renew 已開啟作為安全網）

### D2: S3 Noise — 只保留心理價位避讓

`ApplyNoise()` 移除隨機 rate/amount 擾動，只保留 `avoidPsychLevel()`。`rateNoiseRange` 和 `amountNoiseRange` 常數保留但設為 0。composite 中 `ApplyNoise` 呼叫不變，行為改為只做心理價位偏移。

### D3: M4 Early Return Premium — 加到 ComputeLockupPremium

在 `lockup.go` 的 `ComputeLockupPremium()` 中加入：
```
earlyReturnPremium = earlyReturnBaseRate × (period / 30.0) × 0.05
totalPremium = lockupCost + earlyReturnPremium
```
`earlyReturnBaseRate` 初始為 0.3（30% 歷史提前歸還率估計值），未來由 G10 Performance Tracking 提供實際數據。

### D4: M6 FRR Manipulation Guard — 加到 ComputeBaseRate 和 ComputeFloorRate

兩處使用 FRR 的地方都改為 `effectiveFRR`：
```go
func effectiveFRR(frr float64, bookMidRate float64) float64 {
    return math.Max(frr, bookMidRate * 0.9)
}
```
定義在 `pricing.go` 中作為 package-level helper。

### D5: M8 Feedback Loop — 加到 ComputeBaseRate

`ComputeBaseRate` 新增可選參數或在 composite 中做：如果有 `managedFunding` 和 `totalMarketFunding` 資訊，當 `marketShare > 0.05` 時：
```
frrWeight *= 1.0 - (marketShare - 0.05) * 2.0
```
由於目前 DecisionContext 沒有 marketShare 資訊（需要從 worker 拿），初期在 composite 中以 snapshot 的資訊估算：`marketShare ≈ available / orderBook.AskDepth`。

### D6: S8 Confidence-Scaled Deployment — 加到 composite Stage 3

在 composite.go 的 Stage 3 開頭計算：
```go
avgConf := averageConfidence(snap.Signals)
deployRatio := 0.5 + 0.5*math.Abs(snap.MDC.Score)*avgConf
available *= deployRatio
```
剩餘資金保留在 wallet，下個 tick 可重新評估。

### D7: G11 Idle Capital Urgency — worker 追蹤 + floor 使用

- `worker.go` 新增 `lastLentAt time.Time` 欄位，每次成功執行 offer 時更新
- `DecisionContext` 新增 `IdleMinutes float64` 欄位（由 worker 計算後注入）
- `ComputeFloorRate` 新增 `idleMinutes` 參數：`urgencyDiscount = min(idleMinutes / 120, 0.15)`
- composite 中傳遞 `ctx.IdleMinutes` 到 `ComputeFloorRate`

### D8: M3 Proactive Offer Refresh — 加到 composite Stage 4

在 composite 的 Stage 4 cancels 階段，除了 stale 偵測外，新增 refresh 邏輯：
```go
for _, o := range ctx.ActiveOffers {
    if offerAge(o) > refreshAge && ComputeQueueDiscount(o.Rate, ob) < 1.0 {
        cancels = append(cancels, o.ID)
    }
}
```
`refreshAge = 10 * time.Minute`（可調）。效果：排隊太深且超過 10 分鐘的 offer 會被取消，下個 tick 重新以當前 bestAsk 報價。

## Risks / Trade-offs

- **[G11 DecisionContext 新增欄位]** → 需要改 domain type，但只加一個 float64 欄位，向後相容（零值 = 不啟用）
- **[M8 marketShare 估算不精確]** → `available / AskDepth` 只是粗估，但足以作為 >5% 的門檻判斷
- **[S2 auto-renew 行為改變]** → 從「永遠關閉」改為「永遠開啟」，需確認 Bitfinex API 的 auto-renew 旗標設定方式
