## 1. S3 Remove Random Noise

- [x] 1.1 `noise.go` — 將 `rateNoiseRange` 和 `amountNoiseRange` 設為 0，`ApplyNoise()` 只保留 `avoidPsychLevel()` 邏輯
- [x] 1.2 `composite.go` — 確認 `ApplyNoise` 呼叫仍正常（行為改為只做心理價位偏移）
- [x] 1.3 更新 `noise_test.go` — 驗證不再有隨機擾動，只有心理價位偏移

## 2. M4 Early Return Risk Premium

- [x] 2.1 `lockup.go` — 新增 `earlyReturnBaseRate = 0.30` 常數，在 `ComputeLockupPremium()` 中加入 `earlyReturnPremium = earlyReturnBaseRate × (period / 30.0) × 0.05`，加到 total cost
- [x] 2.2 更新 `lockup_test.go` — 驗證新 premium 被加入

## 3. M6 FRR Manipulation Guard

- [x] 3.1 `pricing.go` — 新增 `EffectiveFRR(frr, bookMidRate float64) float64` 函數
- [x] 3.2 `pricing.go` — `ComputeBaseRate()` 中的 `selectBaseRate()` 改用 `EffectiveFRR`
- [x] 3.3 `floor.go` — `ComputeFloorRate()` 中的 `snap.FRR` 改用 `EffectiveFRR(snap.FRR, snap.OrderBook.MidRate)`
- [x] 3.4 新增測試驗證：FRR 被操縱時 effectiveFRR 使用 bookMidRate

## 4. M8 FRR Feedback Loop Awareness

- [x] 4.1 `composite.go` — Stage 1 開頭計算 `marketShare = available / snap.OrderBook.AskDepth`，超過 5% 時將 base rate 向 bookMidRate 靠攏
- [x] 4.2 新增測試驗證：marketShare > 5% 時 rate 被調整

## 5. S8 Confidence-Scaled Deployment

- [x] 5.1 `composite.go` — Stage 3 開頭新增 `averageConfidence()` helper + `deploymentRatio` 計算，`available *= deploymentRatio`
- [x] 5.2 新增測試驗證：低信心時 offers 的 total amount 低於 available

## 6. G11 Idle Capital Urgency

- [x] 6.1 `domain/decision.go` — `DecisionContext` 新增 `IdleMinutes float64` 欄位
- [x] 6.2 `worker/worker.go` — 新增 `lastLentAt time.Time`，每次成功執行 offer 時更新，tick 時計算 `IdleMinutes` 注入 ctx
- [x] 6.3 `floor.go` — `ComputeFloorRate()` 新增 `idleMinutes float64` 參數，套用 `urgencyDiscount = min(idleMinutes / 120, 0.15)`
- [x] 6.4 `composite.go` — 傳遞 `ctx.IdleMinutes` 到 `ComputeFloorRate()`
- [x] 6.5 新增測試驗證：30 分鐘閒置時 floor 降低約 3.75%

## 7. S2 Auto-Renew Always-On

- [x] 7.1 `execution/credit.go` — 反轉 auto-renew 邏輯：`ProcessExpiring()` 改為先嘗試 pipeline 重定價，失敗時 fallback 到已開啟的 auto-renew
- [x] 7.2 確認 Bitfinex API 中 auto-renew 旗標的設定方式（`flags` 欄位）

## 8. M3 Proactive Offer Refresh

- [x] 8.1 `composite.go` — Stage 4 cancels 階段新增 refresh 邏輯：`offerAge > refreshAge && queueDiscount < 1.0` → 加入 cancels
- [x] 8.2 `composite.go` — 新增 `refreshAge = 10 * time.Minute` 常數
- [x] 8.3 新增測試驗證：老且排隊深的 offer 被 refresh cancel，新的不被取消

## 9. 驗證

- [x] 9.1 `go build ./...` 編譯通過
- [x] 9.2 `go test ./internal/lending/strategy/...` 全部通過
- [x] 9.3 `go test ./internal/lending/worker/...` 全部通過
