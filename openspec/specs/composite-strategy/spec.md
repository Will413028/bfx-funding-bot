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

Rate resolution SHALL compute the final rate using **bestAsk-relative pricing** as the primary method:
1. `rate = bestAsk - tickOffset(MDC, regime)` — offset decreases when bullish, increases when bearish
2. If bestAsk unavailable, fallback to FRR-based pricing
3. `rate = smartWallPosition(rate, walls)` — price just below nearest wall
4. `rate *= (1.0 + FRRTrend × 0.1)` — trend adjustment
5. `rate *= ComputeQueueDiscount()` — sigmoid curve discount
6. Remaining adjustments unchanged (floor, weekend, calendar, lockup, impact)

#### Scenario: BestAsk available with bullish MDC
- **WHEN** bestAsk is 0.0003 and MDC is +0.8 in contango
- **THEN** rate SHALL be close to bestAsk (small offset) rather than FRR-derived

#### Scenario: BestAsk unavailable
- **WHEN** OrderBook.BestAsk is 0 (empty book)
- **THEN** system SHALL fallback to FRR × MDC premium pricing

#### Scenario: Wall nearby — smart positioning
- **WHEN** a large offer wall exists at 0.0004 and computed rate is 0.00039
- **THEN** rate SHALL be set to `0.0004 - minTickSize` to queue just ahead of wall

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
1. Apply **psychological price avoidance only** (no random noise) to each offer's rate
2. Compute partial fill adjustment (amount ±20% based on fill proxy)
3. Collect residual offer cancels from partial fill detection
4. Collect stale offer cancels from queue position (carried from Stage 1)
5. **Collect refresh cancels**: active offers older than `refreshAge` with queue discount < 1.0
6. Remove any offer with amount < $50 after adjustments

#### Scenario: Noise only avoids psychological levels
- **WHEN** noise is applied to an offer rate of 0.0005
- **THEN** rate SHALL be shifted away from the psychological level but NOT randomly perturbed

#### Scenario: Proactive refresh cancels deep-queue old offers
- **WHEN** an active offer is 12 minutes old and its queue discount is 0.98 (moderate)
- **THEN** the offer SHALL be added to cancels for refresh

#### Scenario: Recent offer not refreshed
- **WHEN** an active offer is 3 minutes old even with deep queue
- **THEN** the offer SHALL NOT be cancelled for refresh

### Requirement: DecisionResult reason includes pipeline trace

The Reason field SHALL contain a trace of which stages contributed, formatted as `composite:<stage-details>`.

#### Scenario: Reason with all stages
- **WHEN** all four stages execute normally
- **THEN** Reason SHALL contain key decisions (e.g. "composite:rate=0.00035|period=14|tiers=3|offers=5")

### Requirement: Confidence-scaled deployment ratio

The composite pipeline SHALL scale the deployed capital by signal confidence. When signals are contradictory (low confidence), less capital is deployed.

Formula: `deploymentRatio = 0.5 + 0.5 × |MDC.Score| × avgConfidence`

#### Scenario: High confidence bullish
- **WHEN** MDC score is 0.8 and average signal confidence is 0.9
- **THEN** deploymentRatio SHALL be 0.86 (deploy 86% of available)

#### Scenario: Low confidence neutral
- **WHEN** MDC score is 0.1 and average signal confidence is 0.5
- **THEN** deploymentRatio SHALL be 0.525 (deploy 52.5%, retain 47.5% as reserve)

#### Scenario: Zero signals
- **WHEN** no signals are available (empty slice)
- **THEN** deploymentRatio SHALL be 0.5 (minimum deployment)

### Requirement: Cascade phase response

The composite pipeline SHALL adjust period based on liquidation cascade phase:
- **Early** (0-30min): `period = cfg.Period.Min` — capture spike with shortest lockup
- **Mid** (30min-2hr): `period = min(14, cfg.Period.Max)` — lock in confirmed high rate
- **Late** (2hr+): skip offer generation — rates declining, wait for next opportunity

#### Scenario: Early cascade — short period
- **WHEN** cascade phase is "early" (triggered 10 minutes ago)
- **THEN** period SHALL be cfg.Period.Min

#### Scenario: Late cascade — no new offers
- **WHEN** cascade phase is "late" (triggered 3 hours ago, rates declining)
- **THEN** system SHALL return empty offers with reason "composite:cascade_late"

### Requirement: Dynamic weekend premium

Weekend premium SHALL use historical weekend/weekday rate ratio instead of fixed percentages.
Formula: `weekendMultiplier = rolling4wkWeekendAvg / rolling4wkWeekdayAvg`
Fallback: fixed premiums (1.02-1.05) when historical data insufficient.

#### Scenario: Historical data available
- **WHEN** 4 weeks of rate data exists and weekend rates average 1.3× weekday rates
- **THEN** weekendMultiplier SHALL be 1.3

#### Scenario: No historical data
- **WHEN** less than 4 weeks of data collected
- **THEN** system SHALL use fixed fallback premiums (Friday 1.02, Saturday 1.05, Sunday 1.03)

### Requirement: Rate percentile influences period and deployment

The composite pipeline SHALL use RatePercentile from the snapshot:
- `> 0.75`: extend period toward max (lock in historically high rate)
- `< 0.25`: reduce deploymentRatio by 20% (reserve capital for better opportunity)

#### Scenario: High percentile extends period
- **WHEN** RatePercentile is 0.85 and computed period is 14
- **THEN** period SHALL be extended toward cfg.Period.Max

#### Scenario: Low percentile reduces deployment
- **WHEN** RatePercentile is 0.15
- **THEN** deploymentRatio SHALL be further reduced by 20%

### Requirement: Mean reversion modulates floor urgency

When P(higher_rate) > 0.7, the idle urgency discount (G11) SHALL be halved. When < 0.3, it SHALL be doubled.

#### Scenario: High P(higher) reduces urgency
- **WHEN** P(higher) is 0.8 and idle urgency would discount floor by 10%
- **THEN** actual discount SHALL be 5% (halved — willing to wait)

### Requirement: Gap cost adjusts period preference

Period selection SHALL subtract gap cost penalty from short-period EV, making longer periods more attractive when gap time is significant.

#### Scenario: High gap cost favors longer periods
- **WHEN** avgGapMinutes is 30 and period candidates are 2d and 14d
- **THEN** 2d period's effective return SHALL be penalized by ~1% downtime vs 14d's ~0.15%
