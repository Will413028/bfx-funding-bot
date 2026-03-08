## ADDED Requirements

### Requirement: Impact ratio calculation
The Impact Strategy SHALL compute the impact ratio as (existing offer amount + new amount) / ask depth.

#### Scenario: Calculate impact ratio
- **WHEN** existing offers total 20,000 USD, new amount is 10,000 USD, and ask depth is 200,000 USD
- **THEN** the impact ratio SHALL be (20000 + 10000) / 200000 = 0.15 (15%)

#### Scenario: No existing offers
- **WHEN** there are no existing offers and new amount is 5,000 USD with ask depth 100,000 USD
- **THEN** the impact ratio SHALL be 5000 / 100000 = 0.05 (5%)

### Requirement: Low impact — no adjustment
The Impact Strategy SHALL not adjust rate or amount when the impact ratio is at or below 10%.

#### Scenario: Low impact ratio
- **WHEN** the impact ratio is 0.08 (8%)
- **THEN** the rate and amount SHALL remain unchanged

### Requirement: Moderate impact — rate premium
The Impact Strategy SHALL apply a +3% rate premium when the impact ratio is between 10% and 25%.

#### Scenario: Moderate impact
- **WHEN** the impact ratio is 0.18 (18%) and base rate is 0.0003
- **THEN** the rate SHALL be adjusted to 0.0003 × 1.03

### Requirement: High impact — rate premium and amount reduction
The Impact Strategy SHALL apply a +5% rate premium and reduce the offered amount when the impact ratio exceeds 25%.

#### Scenario: High impact
- **WHEN** existing offers total 30,000 USD and ask depth is 200,000 USD
- **THEN** the maximum additional amount SHALL be 25% × 200000 - 30000 = 20,000 USD, and rate SHALL be adjusted by +5%

#### Scenario: High impact with amount fully consumed
- **WHEN** existing offers already exceed 25% of ask depth
- **THEN** the system SHALL return an empty DecisionResult with reason "impact:over_limit"

### Requirement: Zero ask depth handling
#### Scenario: Ask depth is zero
- **WHEN** ask depth is 0
- **THEN** the system SHALL skip impact adjustment (treat as low impact)

### Requirement: Flash freeze protection
#### Scenario: Flash freeze active
- **WHEN** the market snapshot has FlashFreeze = true
- **THEN** the system SHALL return an empty DecisionResult with reason "flash_freeze"

### Requirement: Insufficient balance handling
#### Scenario: Balance below minimum
- **WHEN** the available balance is less than 50 USD
- **THEN** the system SHALL return an empty DecisionResult with reason "insufficient_balance"
