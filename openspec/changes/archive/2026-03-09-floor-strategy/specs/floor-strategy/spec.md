## ADDED Requirements

### Requirement: Opportunity cost floor
The Floor Strategy SHALL use the user's configured minimum rate (`config.Rate.Min`) as the baseline opportunity cost floor.

#### Scenario: Config minimum as floor baseline
- **WHEN** the user's config has `Rate.Min = 0.0001`
- **THEN** the opportunity cost floor SHALL be 0.0001

### Requirement: FRR relative floor
The Floor Strategy SHALL compute a floor based on the market's Flash Return Rate (FRR), applying a ratio factor (default 0.8).

#### Scenario: FRR available
- **WHEN** the market snapshot has FRR = 0.00025
- **THEN** the FRR relative floor SHALL be 0.00025 × 0.8 = 0.0002

#### Scenario: FRR is zero
- **WHEN** the market snapshot has FRR = 0
- **THEN** the FRR relative floor SHALL be 0 (this layer does not contribute)

### Requirement: Regime-adjusted floor
The Floor Strategy SHALL adjust the floor based on the current market regime to increase protection during adverse conditions.

#### Scenario: Crisis regime
- **WHEN** the market regime is "crisis"
- **THEN** the regime floor SHALL be `config.Rate.Min × 1.5`

#### Scenario: Backwardation regime
- **WHEN** the market regime is "backwardation"
- **THEN** the regime floor SHALL be `config.Rate.Min × 1.2`

#### Scenario: Contango regime
- **WHEN** the market regime is "contango"
- **THEN** the regime floor SHALL be `config.Rate.Min × 1.0` (no adjustment)

#### Scenario: Neutral regime
- **WHEN** the market regime is "neutral"
- **THEN** the regime floor SHALL be `config.Rate.Min × 1.0` (no adjustment)

### Requirement: Floor is maximum of all layers
The final floor rate SHALL be the maximum of all computed floor values (opportunity cost, FRR relative, regime-adjusted).

#### Scenario: FRR floor is highest
- **WHEN** FRR relative floor (0.0002) exceeds both opportunity cost (0.0001) and regime floor (0.0001)
- **THEN** the final floor SHALL be 0.0002

#### Scenario: Regime floor is highest
- **WHEN** regime is "crisis" and regime floor (0.00015) exceeds FRR floor (0.0002 × 0.8 = 0.00016) and opportunity cost (0.0001)
- **THEN** the final floor SHALL be the regime floor

### Requirement: Flash freeze protection
The Floor Strategy SHALL produce no offers when a flash crash is detected.

#### Scenario: Flash freeze active
- **WHEN** the market snapshot has FlashFreeze = true
- **THEN** the system SHALL return an empty DecisionResult with reason "flash_freeze"

### Requirement: Insufficient balance handling
The Floor Strategy SHALL produce no offers when available balance is below the minimum threshold (50 USD).

#### Scenario: Balance below minimum
- **WHEN** the available balance is less than 50 USD
- **THEN** the system SHALL return an empty DecisionResult with reason "insufficient_balance"

### Requirement: Floor output format
The Floor Strategy SHALL return a DecisionResult with the computed floor rate and a reason indicating which floor layer was dominant.

#### Scenario: Normal floor output
- **WHEN** the floor is computed successfully
- **THEN** the result SHALL contain one OfferDecision with the floor rate, and the Reason SHALL indicate the dominant floor source (e.g., "floor:frr_relative", "floor:regime", "floor:opportunity_cost")
