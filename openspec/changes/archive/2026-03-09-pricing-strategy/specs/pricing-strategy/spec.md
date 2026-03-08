## ADDED Requirements

### Requirement: MDC-based premium calculation
The Pricing Strategy SHALL compute a premium multiplier based on the MDC (Market Demand Curve) score. The MDC score ranges from -1 (extreme supply) to +1 (extreme demand). The multiplier SHALL be linearly interpolated between a minimum premium (0.7 at MDC=-1) and a maximum premium (1.5 at MDC=+1), with 1.0 at MDC=0.

#### Scenario: High demand (positive MDC)
- **WHEN** MDC score is +0.8
- **THEN** the premium multiplier SHALL be 1.0 + 0.8 × 0.5 = 1.4 (linear interpolation toward max 1.5)

#### Scenario: High supply (negative MDC)
- **WHEN** MDC score is -0.6
- **THEN** the premium multiplier SHALL be 1.0 + (-0.6) × 0.3 = 0.82 (linear interpolation toward min 0.7)

#### Scenario: Balanced market (MDC near zero)
- **WHEN** MDC score is 0.0
- **THEN** the premium multiplier SHALL be 1.0 (no premium adjustment)

### Requirement: Base rate from FRR
The Pricing Strategy SHALL use the Flash Return Rate (FRR) from the market ticker as the base rate for premium calculations.

#### Scenario: FRR available
- **WHEN** the market snapshot contains a valid FRR value
- **THEN** the system SHALL use FRR as the base rate before applying premium adjustments

#### Scenario: FRR is zero or unavailable
- **WHEN** the FRR is zero
- **THEN** the system SHALL fall back to the order book mid-rate as the base rate

### Requirement: Regime-based rate adjustment
The Pricing Strategy SHALL adjust the calculated rate based on the current market regime.

#### Scenario: Contango regime
- **WHEN** the market regime is "contango"
- **THEN** the system SHALL apply a +5% multiplier to the premium-adjusted rate (rates trending up)

#### Scenario: Backwardation regime
- **WHEN** the market regime is "backwardation"
- **THEN** the system SHALL apply a -10% multiplier to the premium-adjusted rate (rates trending down)

#### Scenario: Crisis regime
- **WHEN** the market regime is "crisis"
- **THEN** the system SHALL use the user's configured minimum rate (config.Rate.Min) regardless of MDC or other calculations

#### Scenario: Neutral regime
- **WHEN** the market regime is "neutral"
- **THEN** the system SHALL not apply any regime adjustment (multiplier = 1.0)

### Requirement: Order book pressure adjustment
The Pricing Strategy SHALL make minor adjustments based on order book bid/ask depth ratio.

#### Scenario: Strong borrowing pressure
- **WHEN** the bid depth / ask depth ratio exceeds 1.5
- **THEN** the system SHALL increase the rate by 2% (borrowers competing for funds)

#### Scenario: Normal depth ratio
- **WHEN** the bid/ask depth ratio is between 0.67 and 1.5
- **THEN** the system SHALL not apply any depth adjustment

### Requirement: Wall avoidance
The Pricing Strategy SHALL reduce the rate slightly when a large offer wall is detected near the calculated rate.

#### Scenario: Offer wall near calculated rate
- **WHEN** an offer wall exists within 10% of the calculated rate
- **THEN** the system SHALL reduce the rate by 1% to improve queue position

#### Scenario: No nearby walls
- **WHEN** no offer walls exist near the calculated rate
- **THEN** the system SHALL not apply any wall adjustment

### Requirement: Rate clamping
The final calculated rate SHALL always be clamped to the user's configured rate bounds.

#### Scenario: Rate within bounds
- **WHEN** the calculated rate is between config.Rate.Min and config.Rate.Max
- **THEN** the system SHALL use the calculated rate as-is

#### Scenario: Rate below minimum
- **WHEN** the calculated rate falls below config.Rate.Min
- **THEN** the system SHALL use config.Rate.Min

#### Scenario: Rate above maximum
- **WHEN** the calculated rate exceeds config.Rate.Max
- **THEN** the system SHALL use config.Rate.Max

### Requirement: Flash freeze protection
The Pricing Strategy SHALL produce no offers when a flash crash is detected.

#### Scenario: Flash freeze active
- **WHEN** the market snapshot has FlashFreeze = true
- **THEN** the system SHALL return an empty DecisionResult with zero offers and reason "flash_freeze"

### Requirement: Insufficient balance handling
The Pricing Strategy SHALL produce no offers when available balance is below the minimum threshold.

#### Scenario: Balance below minimum
- **WHEN** the available balance is less than 50 USD
- **THEN** the system SHALL return an empty DecisionResult with reason "insufficient_balance"
