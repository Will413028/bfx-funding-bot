## Why

G0（pipeline 接線）和 GT1-GT5（參數調校）完成後，P1 層級的 8 項低複雜度改進是下一個收益提升點。這些項目多數只需幾行改動，但合計影響顯著：消除宕機資金閒置、減少 noise 損耗、加入提前歸還溢價、防 FRR 操縱、信心度控制部署比例、閒置資金逐步降低 floor、主動搶隊首。

## What Changes

- **S2**：Auto-renew 邏輯反轉 — 預設開啟，引擎主動管理到期
- **S3**：移除隨機 noise，保留心理價位避讓。ApplyNoise 改為只加正向偏移
- **M4**：lockup 計算加入提前歸還風險溢價（callable risk premium）
- **M6**：floor/pricing 使用 `effectiveFRR = max(FRR, bookMidRate × 0.9)` 防操縱
- **M8**：pricing 加入 marketShare 感知，>5% 時降低 FRR 權重
- **S8**：composite pipeline 加入 `deploymentRatio = 0.5 + 0.5 × |MDC| × avgConfidence`
- **G11**：worker 追蹤 `lastLentAt`，floor 加入 `urgencyDiscount`
- **M3**：worker/composite 加入 proactive offer refresh 邏輯

## Capabilities

### New Capabilities

- `idle-capital-urgency`: G11 閒置資金急迫度 — worker 追蹤閒置時間，floor 隨時間降低

### Modified Capabilities

- `composite-strategy`: S3 noise 改為正向、S8 信心度部署比例、M3 offer refresh
- `lending-strategy`: S2 auto-renew 反轉、M4 提前歸還溢價、M6 FRR 防操縱、M8 反饋迴路

## Impact

- **涉及檔案**：`strategy/noise.go`, `strategy/lockup.go`, `strategy/floor.go`, `strategy/pricing.go`, `strategy/composite.go`, `strategy/queue.go`, `execution/credit.go`, `worker/worker.go`
- **介面不變**：所有改動都在現有函數內部，不改 interface
- **風險低**：每項改動獨立，互不依賴，可逐一驗證
