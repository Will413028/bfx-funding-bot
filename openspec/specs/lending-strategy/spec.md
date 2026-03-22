## ADDED Requirements

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

### Requirement: Stale offer cancellation (Zombie TTL)
The system SHALL cancel active offers that have not been filled within 15 minutes (spec §7.1 simplified).

#### Scenario: Offer exceeds TTL
- **WHEN** an active offer has been pending for more than 15 minutes
- **THEN** the system SHALL cancel the offer, allowing the next tick to resubmit with potentially updated parameters

#### Scenario: Offer within TTL
- **WHEN** an active offer has been pending for less than 15 minutes
- **THEN** the system SHALL leave the offer unchanged

### Requirement: Interest reinvestment
The system SHALL automatically include newly settled interest in the lending pool (spec §7.1).

#### Scenario: Interest accumulates to lendable amount
- **WHEN** the available balance (including settled interest) reaches >= 50 USD after previous offers are filled
- **THEN** the system SHALL submit a new offer for the available amount on the next tick

### Requirement: Minimum balance safety guard
The system SHALL not submit offers when the available balance is below the minimum threshold (spec §8.2).

#### Scenario: Balance below minimum
- **WHEN** the funding wallet available balance is < 50 USD
- **THEN** the system SHALL not submit any offers and SHALL log the skip reason

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

### Requirement: Auto-renew as safety net

The lending engine SHALL keep auto-renew **enabled** on all active credits as a safety net. When the engine is healthy, it SHALL proactively manage credit expiry by cancelling auto-renew and re-pricing via the composite pipeline before expiry. Auto-renew only activates when the engine fails to act.

#### Scenario: Engine healthy at credit expiry
- **WHEN** a credit is about to expire and the engine is running normally
- **THEN** the engine SHALL cancel auto-renew, run the pricing pipeline, and place a new offer with the computed rate

#### Scenario: Engine down at credit expiry
- **WHEN** a credit expires while the engine is not running
- **THEN** Bitfinex auto-renew SHALL activate automatically, preventing capital idle

### Requirement: Early return risk premium on lockup cost

The lockup cost calculation SHALL include a premium for early return risk (callable bond negative convexity). Longer periods and higher historical early return rates incur higher premiums.

Formula: `earlyReturnPremium = earlyReturnBaseRate × (period / 30.0) × 0.05`

#### Scenario: 30-day period with 30% early return rate
- **WHEN** period is 30 days and earlyReturnBaseRate is 0.3
- **THEN** earlyReturnPremium SHALL be 0.005 (0.3 × 1.0 × 0.05)

#### Scenario: 2-day period — minimal premium
- **WHEN** period is 2 days
- **THEN** earlyReturnPremium SHALL be 0.001 (0.3 × 0.067 × 0.05), negligible

### Requirement: FRR manipulation guard

All strategy calculations using FRR SHALL use an effective FRR that guards against manipulation:
`effectiveFRR = max(FRR, bookMidRate × 0.9)`

#### Scenario: FRR significantly below book mid rate
- **WHEN** FRR is 0.0001 and bookMidRate is 0.0003
- **THEN** effectiveFRR SHALL be 0.00027 (0.0003 × 0.9), not 0.0001

#### Scenario: FRR consistent with book
- **WHEN** FRR is 0.0003 and bookMidRate is 0.0003
- **THEN** effectiveFRR SHALL be 0.0003 (FRR wins, no adjustment)

### Requirement: FRR feedback loop awareness

When the user's managed funding represents a significant fraction of the market, the pricing strategy SHALL reduce FRR's influence to prevent positive feedback loops.

`marketShare ≈ available / orderBook.AskDepth`
When `marketShare > 0.05`: `frrWeight *= 1.0 - (marketShare - 0.05) × 2.0`

#### Scenario: 10% market share
- **WHEN** available is $10,000 and AskDepth is $100,000
- **THEN** frrWeight SHALL be reduced by 10% (marketShare=0.10, reduction = (0.10-0.05)×2 = 0.10)

#### Scenario: 2% market share — no adjustment
- **WHEN** available is $2,000 and AskDepth is $100,000
- **THEN** frrWeight SHALL not be adjusted (marketShare=0.02, below 5% threshold)

### Requirement: Smart wall positioning

When a large offer wall is detected near the computed rate, the system SHALL position the offer at `wallRate - minTickSize` instead of applying a fixed discount. This queues the offer just ahead of the wall for faster fill.

#### Scenario: Wall within proximity
- **WHEN** computed rate is 0.00039 and an offer wall exists at 0.0004
- **THEN** rate SHALL be adjusted to `0.0004 - 0.00000001` (wall rate minus 1 tick)

#### Scenario: No wall nearby
- **WHEN** no offer wall is within 10% of the computed rate
- **THEN** rate SHALL remain unchanged

### Requirement: Sigmoid queue discount

Queue position discount SHALL use a smoothstep sigmoid curve instead of discrete thresholds, providing continuous adjustment across the full queue depth range.

Formula: `discount = 1.0 - maxDiscount × smoothstep(queueRatio, 0.1, 0.6)`

#### Scenario: Queue ratio at 0.35 (was between thresholds)
- **WHEN** queueRatio is 0.35 (previously between 0.20 and 0.50 thresholds)
- **THEN** discount SHALL be a smooth intermediate value (~0.985) instead of jumping from 0.98 to 0.95

#### Scenario: Queue ratio at 0.05 (below range)
- **WHEN** queueRatio is 0.05
- **THEN** discount SHALL be 1.0 (no adjustment)

#### Scenario: Queue ratio at 0.70 (above range)
- **WHEN** queueRatio is 0.70
- **THEN** discount SHALL be 0.95 (maximum discount)

### Requirement: Auto-renew re-pricing via pipeline

When a credit is about to expire, the system SHALL compute a new rate and period via the composite strategy pipeline instead of renewing at the original parameters.

#### Scenario: Market rate increased since original offer
- **WHEN** a credit at 0.0002 rate expires and current market rate is 0.0004
- **THEN** the renewal offer SHALL use the pipeline-computed rate (~0.0004) instead of 0.0002

#### Scenario: Pipeline failure — fallback to auto-renew
- **WHEN** the pipeline fails to produce valid offers for a renewing credit
- **THEN** the system SHALL fall back to Bitfinex auto-renew (already enabled as safety net)

### Requirement: Order book gap pricing

When the computed offer rate falls within a detected order book gap, the pricing module SHALL adjust to the gap's low end for fastest fill.

#### Scenario: Rate in gap
- **WHEN** bestAsk-relative pricing produces rate 0.00040 and a gap exists [0.00035, 0.00045]
- **THEN** rate SHALL be adjusted to 0.00035 (no competition at this level)

#### Scenario: Rate not in any gap
- **WHEN** computed rate has neighboring offers on both sides
- **THEN** rate SHALL remain unchanged
