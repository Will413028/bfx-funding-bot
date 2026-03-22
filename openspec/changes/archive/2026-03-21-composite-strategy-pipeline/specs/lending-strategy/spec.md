## MODIFIED Requirements

### Requirement: Basic lending strategy execution
The system SHALL execute a lending strategy for each active user based on their `StrategyConfig`. The strategy SHALL check available balance, manage active offers, and submit new offers when appropriate. **策略執行由 CompositeStrategy pipeline 驅動，而非單一 PricingStrategy。**

#### Scenario: Submit new offer when balance available
- **WHEN** the user's funding wallet has available balance >= 50 USD AND there are no active offers
- **THEN** the system SHALL submit new funding offer(s) via CompositeStrategy pipeline, which computes rate from MDC + regime + floor + premiums, period from regime + volatility, and may produce multiple offers via allocation tiers and depth-aware splitting

#### Scenario: Skip when balance too low
- **WHEN** the user's funding wallet has available balance < 50 USD
- **THEN** the system SHALL skip offer submission for this user

#### Scenario: Skip when active offer exists and not stale
- **WHEN** the user has an active offer that was created less than 15 minutes ago
- **THEN** the system SHALL skip offer submission (keep existing offer)

### Requirement: Offer parameters from strategy config
The system SHALL use the user's `StrategyConfig` to determine offer parameter **bounds**, with actual values computed by the CompositeStrategy pipeline.

#### Scenario: Rate selection
- **WHEN** submitting an offer
- **THEN** the system SHALL compute rate via the CompositeStrategy pipeline (base rate from MDC + FRR, floor enforcement, weekend/calendar premiums, lockup cost, queue discount, market impact premium), clamped to [config.Rate.Min, config.Rate.Max]

#### Scenario: Amount selection
- **WHEN** submitting an offer
- **THEN** the system SHALL compute amount via allocation tiers (1-3 tiers based on balance and regime) and depth-aware splitting (max 5 orders per tier, each ≤ 5% of ask depth), with each offer amount ≥ 50 USD

#### Scenario: Period selection
- **WHEN** submitting an offer
- **THEN** the system SHALL compute period via regime-driven ratio + rate scaling + volatility discount + stagger adjustment, clamped to [config.Period.Min, config.Period.Max]

#### Scenario: Currency selection
- **WHEN** submitting an offer
- **THEN** the system SHALL use `config.Currency` as the offer currency (prefixed with "f" for Bitfinex symbol)

## ADDED Requirements

### Requirement: Strategy modules expose helper functions

Each of the 13 strategy modules SHALL export a pure computation function that the CompositeStrategy can call independently of the full `Apply()` method.

#### Scenario: PricingStrategy exports ComputeBaseRate
- **WHEN** CompositeStrategy needs the MDC-adjusted base rate
- **THEN** it SHALL call `ComputeBaseRate(snap, cfg)` which returns the rate without building a full DecisionResult

#### Scenario: FloorStrategy exports ComputeFloorRate
- **WHEN** CompositeStrategy needs the floor rate
- **THEN** it SHALL call `ComputeFloorRate(snap, cfg)` which returns max(opportunity cost, FRR relative, regime floor)

#### Scenario: WeekendPremiumStrategy exports ComputeWeekendMultiplier
- **WHEN** CompositeStrategy needs the weekend premium
- **THEN** it SHALL call `ComputeWeekendMultiplier(t)` which returns the multiplier (1.0 for weekdays, 1.02-1.05 for weekends)

#### Scenario: Helper functions are pure
- **WHEN** any helper function is called
- **THEN** it SHALL not modify any state, read only from its parameters, and return a deterministic result (except NoiseStrategy which uses randomness)
