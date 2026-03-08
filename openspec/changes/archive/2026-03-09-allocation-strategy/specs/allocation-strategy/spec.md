## ADDED Requirements

### Requirement: Tier count based on available balance
The Allocation Strategy SHALL determine the number of funding tiers based on the available balance.

#### Scenario: Small balance (single tier)
- **WHEN** available balance is less than 150 USD
- **THEN** the system SHALL produce 1 tier using the full available amount

#### Scenario: Medium balance (two tiers)
- **WHEN** available balance is between 150 and 1000 USD (inclusive)
- **THEN** the system SHALL produce 2 tiers: core (70%) and aggressive (30%)

#### Scenario: Large balance (three tiers)
- **WHEN** available balance exceeds 1000 USD
- **THEN** the system SHALL produce 3 tiers: core (50%), moderate (30%), aggressive (20%)

### Requirement: Minimum amount per tier
Each tier's allocated amount SHALL be at least 50 USD. If splitting would produce a tier below 50 USD, the tier count SHALL be reduced.

#### Scenario: Two-tier split produces sub-minimum
- **WHEN** available balance is 120 USD and 30% tier would be 36 USD (< 50)
- **THEN** the system SHALL fall back to 1 tier

### Requirement: Tier rate multipliers
Each tier SHALL apply a rate multiplier to the base rate to create differentiated offers.

#### Scenario: Core tier
- **WHEN** a core tier offer is generated
- **THEN** the rate multiplier SHALL be 1.0 (base rate, prioritize fill)

#### Scenario: Moderate tier
- **WHEN** a moderate tier offer is generated
- **THEN** the rate multiplier SHALL be 1.1 (+10%)

#### Scenario: Aggressive tier
- **WHEN** an aggressive tier offer is generated
- **THEN** the rate multiplier SHALL be 1.25 (+25%)

### Requirement: Regime-adjusted tier count
The Allocation Strategy SHALL adjust the maximum number of tiers based on market regime.

#### Scenario: Crisis regime
- **WHEN** the market regime is "crisis"
- **THEN** the system SHALL force 1 tier regardless of balance (capital preservation)

#### Scenario: Backwardation regime
- **WHEN** the market regime is "backwardation"
- **THEN** the system SHALL allow at most 2 tiers (reduce aggressive exposure)

#### Scenario: Contango or neutral regime
- **WHEN** the market regime is "contango" or "neutral"
- **THEN** the system SHALL allow normal tier count based on balance

### Requirement: Flash freeze protection
#### Scenario: Flash freeze active
- **WHEN** the market snapshot has FlashFreeze = true
- **THEN** the system SHALL return an empty DecisionResult with reason "flash_freeze"

### Requirement: Insufficient balance handling
#### Scenario: Balance below minimum
- **WHEN** the available balance is less than 50 USD
- **THEN** the system SHALL return an empty DecisionResult with reason "insufficient_balance"
