### Requirement: CompositeStrategy implements worker.Strategy interface

CompositeStrategy SHALL implement `Apply(ctx *domain.DecisionContext) *domain.DecisionResult`，與所有獨立策略使用相同的 interface。

#### Scenario: CompositeStrategy is used as worker.Strategy
- **WHEN** `factory.go` creates worker deps
- **THEN** `Strategy` field SHALL be a `CompositeStrategy` instance

### Requirement: Pipeline executes in four stages

CompositeStrategy.Apply() SHALL 按以下順序執行四個階段：
1. Rate Resolution — 計算最終 rate
2. Period Resolution — 計算最終 period
3. Offer Structure — 分層 + 拆分產出 offers
4. Final Adjustments — noise 擾動 + partial fill 調整 + cancels 收集

#### Scenario: Full pipeline execution
- **WHEN** Apply() is called with valid DecisionContext (snapshot non-nil, no flash freeze, available >= $50)
- **THEN** system SHALL execute all four stages in order and return a DecisionResult with computed offers and cancels

#### Scenario: Guard check — snapshot nil
- **WHEN** Apply() is called with nil Snapshot
- **THEN** system SHALL return DecisionResult with empty offers and Reason "composite:no_snapshot"

#### Scenario: Guard check — flash freeze
- **WHEN** Apply() is called with Snapshot.FlashFreeze == true
- **THEN** system SHALL return DecisionResult with empty offers and Reason "composite:flash_freeze"

#### Scenario: Guard check — insufficient balance
- **WHEN** Apply() is called with Available < 50.0
- **THEN** system SHALL return DecisionResult with empty offers and Reason "composite:low_balance"

### Requirement: Stage 1 Rate Resolution

Rate resolution SHALL compute the final rate by combining outputs from multiple strategy helpers in order:
1. `ComputeBaseRate()` → base rate from FRR + MDC premium + regime + depth + walls
2. `ComputeFloorRate()` → three-layer floor (opportunity cost, FRR relative, regime)
3. `rate = max(baseRate, floorRate)`
4. `rate *= ComputeWeekendMultiplier()` — weekend premium
5. `rate *= ComputeCalendarMultiplier()` — calendar event premium (may shorten period)
6. `rate += ComputeLockupPremium()` — lockup cost (may shorten period)
7. `rate *= ComputeQueueDiscount()` — queue position discount (collects stale cancels)
8. `rate *= ComputeImpactMultiplier()` — market impact premium (may reduce amount)
9. `rate = clamp(rate, Config.Rate.Min, Config.Rate.Max)`

#### Scenario: Floor enforced above pricing
- **WHEN** ComputeBaseRate returns 0.0001 and ComputeFloorRate returns 0.00015
- **THEN** rate SHALL be 0.00015 (floor wins)

#### Scenario: Rate clamped to config bounds
- **WHEN** computed rate exceeds Config.Rate.Max
- **THEN** final rate SHALL equal Config.Rate.Max

### Requirement: Stage 2 Period Resolution

Period resolution SHALL compute the final period by:
1. `ComputePeriod()` → regime-driven period with rate scaling and volatility discount
2. `AdjustPeriodForStagger()` → analyze active credit expiry distribution, prefer underrepresented days
3. Period from calendar event shortening (Stage 1) or lockup cost shortening takes precedence if shorter

#### Scenario: Stagger adjusts period
- **WHEN** 60% of active credits expire on day 7 and ComputePeriod returns 7
- **THEN** AdjustPeriodForStagger SHALL return a different day to reduce concentration

### Requirement: Stage 3 Offer Structure

Offer structure SHALL:
1. Call `ComputeTiers()` to split available amount into 1-3 tiers (based on balance + regime)
2. For each tier, multiply the resolved rate by the tier's rate multiplier
3. For each tier, call `ComputeSplits()` to further split if amount > 5% of ask depth
4. Each split becomes an OfferDecision in the final result

#### Scenario: Three-tier allocation with splitting
- **WHEN** available balance is $2000 and ask depth is $5000
- **THEN** system SHALL produce 3 tiers (50%/30%/20%) and further split any tier > $250 (5% of $5000)

### Requirement: Stage 4 Final Adjustments

Final adjustments SHALL:
1. Apply noise perturbation to each offer's rate and amount
2. Compute partial fill adjustment (amount ±20% based on fill proxy)
3. Collect residual offer cancels from partial fill detection
4. Collect stale offer cancels from queue position (carried from Stage 1)
5. Remove any offer with amount < $50 after adjustments

#### Scenario: Offer removed after noise reduces amount below minimum
- **WHEN** noise reduces an offer amount to $45
- **THEN** that offer SHALL be excluded from the final result

### Requirement: DecisionResult reason includes pipeline trace

The Reason field SHALL contain a trace of which stages contributed, formatted as `composite:<stage-details>`.

#### Scenario: Reason with all stages
- **WHEN** all four stages execute normally
- **THEN** Reason SHALL contain key decisions (e.g. "composite:rate=0.00035|period=14|tiers=3|offers=5")
