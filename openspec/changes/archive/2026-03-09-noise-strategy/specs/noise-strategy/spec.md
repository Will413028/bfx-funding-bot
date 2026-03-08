## ADDED Requirements

### Requirement: Rate noise
The Noise Strategy SHALL add a random perturbation within ±1% to the rate.

#### Scenario: Rate perturbation
- **WHEN** the base rate is 0.0003
- **THEN** the adjusted rate SHALL be within [0.0003 × 0.99, 0.0003 × 1.01]

### Requirement: Amount noise
The Noise Strategy SHALL add a random perturbation within ±2% to the amount.

#### Scenario: Amount perturbation
- **WHEN** the base amount is 5000
- **THEN** the adjusted amount SHALL be within [5000 × 0.98, 5000 × 1.02]

### Requirement: Psychological price avoidance
The Noise Strategy SHALL shift the rate away from multiples of 0.0005 by adding 0.00002 when the rate is within 0.00001 of such a multiple.

#### Scenario: Rate near round number
- **WHEN** the adjusted rate is 0.000500 (exactly a multiple of 0.0005)
- **THEN** the rate SHALL be shifted to 0.000520

#### Scenario: Rate not near round number
- **WHEN** the adjusted rate is 0.000430
- **THEN** the rate SHALL NOT be shifted

### Requirement: Rate clamping
The Noise Strategy SHALL clamp the final rate within config rate bounds after all adjustments.

#### Scenario: Noise pushes below min
- **WHEN** noise pushes rate below config.Rate.Min
- **THEN** the rate SHALL be clamped to config.Rate.Min

### Requirement: Amount bounds
The Noise Strategy SHALL ensure the final amount does not exceed config.Amount.Max or available balance, and is at least minBalance.

#### Scenario: Amount exceeds max
- **WHEN** noise pushes amount above config.Amount.Max
- **THEN** the amount SHALL be clamped to config.Amount.Max

### Requirement: Flash freeze protection
#### Scenario: Flash freeze active
- **WHEN** the market snapshot has FlashFreeze = true
- **THEN** the system SHALL return an empty DecisionResult with reason "flash_freeze"

### Requirement: Insufficient balance handling
#### Scenario: Balance below minimum
- **WHEN** the available balance is less than 50 USD
- **THEN** the system SHALL return an empty DecisionResult with reason "insufficient_balance"
