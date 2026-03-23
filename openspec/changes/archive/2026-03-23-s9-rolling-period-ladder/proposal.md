## Why

目前 CompositeStrategy 對同一次決策的所有 offer 指定**相同的 period**。這導致大量債權在同一天到期，形成集中風險：到期日當天若市場利率下降，所有資金同時暴露於低利率環境。

現有的 `stagger.go` 是被動式的——只在偵測到到期集中時微調 period，但不主動維護梯隊分布。結合已完成的 S7（per-currency preset）和 allocation tier system（已有 core/moderate/aggressive 三層），是時候讓 period 也分層：1/3 短期（高流動性）+ 1/3 中期（平衡）+ 1/3 長期（鎖定收益），並在續約時根據 RatePercentile 動態調整。

## What Changes

- 定義三個 period tier：Short（Period.Min ~ 1/3 range）、Medium（1/3 ~ 2/3 range）、Long（2/3 ~ Period.Max）
- CompositeStrategy Stage 3（Offer Structure）改為：每個 allocation tier 對應一個 period tier，而非統一 period
- 新增 `PeriodLadder` 結構，計算各 tier 的目標 period，並考慮既有 credit 的到期分布避免碰撞
- 續約決策整合 RatePercentile：> P75 → 升級為長期；< P25 → 降級為短期；中間維持
- 保持向後相容：若 `Period.Min == Period.Max`（使用者鎖定單一天數），梯隊退化為單一 period

## Capabilities

### New Capabilities

- `period-ladder`: 到期梯隊管理 — 三層 period 分配（short/medium/long），credit-aware 碰撞避免，RatePercentile 驅動的續約升降級

### Modified Capabilities

## Impact

- `internal/lending/strategy/period.go` — 新增 `ComputePeriodLadder()` 取代單一 `ComputePeriod()`
- `internal/lending/strategy/stagger.go` — 整合梯隊 aware 的碰撞避免邏輯
- `internal/lending/strategy/composite.go` — Stage 3 改為 per-tier period 分配
- `internal/lending/strategy/allocation.go` — 微調：tier 結構新增 period 欄位
