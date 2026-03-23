## ADDED Requirements

### Requirement: Period Tier 定義
系統 SHALL 將使用者的 `[Period.Min, Period.Max]` 範圍等分為三個 period tier：Short（前 1/3）、Medium（中 1/3）、Long（後 1/3）。當 `Max - Min < 3` 時，SHALL 退化為單一 period（現有行為）。

#### Scenario: 正常範圍分層
- **WHEN** Period.Min=2, Period.Max=30
- **THEN** Short=[2,11], Medium=[12,20], Long=[21,30]

#### Scenario: 範圍太小退化
- **WHEN** Period.Min=2, Period.Max=4（range=2 < 3）
- **THEN** 所有 offer MUST 使用現有的單一 period 計算邏輯

#### Scenario: Min 等於 Max
- **WHEN** Period.Min=7, Period.Max=7
- **THEN** 所有 offer 的 period MUST 為 7

### Requirement: Allocation Tier 與 Period Tier 映射
系統 SHALL 將 allocation tier 與 period tier 1:1 映射：Core→Short, Moderate→Medium, Aggressive→Long。若只有 1 個 allocation tier，SHALL 使用 Medium period tier。若有 2 個 tier，SHALL 使用 Short 和 Medium。

#### Scenario: 三層映射
- **WHEN** 餘額 > 1000 且 regime=Contango（3 tiers）
- **THEN** Core offer 的 period MUST 在 Short 範圍，Moderate 在 Medium，Aggressive 在 Long

#### Scenario: 單層退化
- **WHEN** 餘額 < 150 或 regime=Crisis（1 tier）
- **THEN** 唯一 offer 的 period MUST 在 Medium 範圍

#### Scenario: 雙層映射
- **WHEN** 餘額 150-1000 或 regime=Backwardation（2 tiers）
- **THEN** Core 在 Short，Aggressive 在 Medium

### Requirement: RatePercentile 偏移
系統 SHALL 根據 `RatePercentile` 偏移各 tier 的 period。> 0.5 向 Long 偏移（鎖定高利率），< -0.5 向 Short 偏移（保持靈活），偏移量 MUST 不超過 tier range 的 30%。

#### Scenario: 高利率鎖定
- **WHEN** RatePercentile=0.8（P90 附近）且 tier=Long, range=[21,30]
- **THEN** period MUST 向 30 偏移（增加 0.8 × 9 × 0.3 ≈ 2 天）

#### Scenario: 低利率保持靈活
- **WHEN** RatePercentile=-0.8（P10 附近）且 tier=Short, range=[2,11]
- **THEN** period MUST 向 2 偏移（減少天數）

#### Scenario: 中間利率不偏移
- **WHEN** RatePercentile=0.0（P50）
- **THEN** 各 tier period MUST 不受 RatePercentile 偏移影響

### Requirement: Tier 內碰撞避免
系統 SHALL 在每個 period tier 的子範圍內執行到期碰撞避免。SHALL 分析 `ActiveCredits` 的到期分布，在該 tier 的範圍內選擇到期密度最低的天數。

#### Scenario: 避開集中到期日
- **WHEN** tier=Medium, range=[12,20], 既有 credit 集中在 day 14 和 15
- **THEN** period MUST 避開 14 和 15，選擇 range 內密度最低的天數

#### Scenario: 無既有 credit
- **WHEN** ActiveCredits 為空
- **THEN** period MUST 使用 tier 中間值（regime/rate/volatility 調整後）

### Requirement: ComputePeriodLadder 函式
系統 SHALL 提供 `ComputePeriodLadder()` 函式，接受 regime、volatility、rate、FRR、config、RatePercentile、ActiveCredits、tier count，回傳 `PeriodLadder`（每個 tier 的 period int 陣列）。

#### Scenario: 完整三層梯隊
- **WHEN** 3 tiers, Period.Min=2, Period.Max=30, regime=Neutral, RatePercentile=0.0
- **THEN** 回傳 3 個不同的 period 值，分別落在 Short/Medium/Long 範圍內

#### Scenario: 向後相容退化
- **WHEN** 1 tier 且 Period.Min==Period.Max
- **THEN** 回傳 1 個 period 等於 Min
