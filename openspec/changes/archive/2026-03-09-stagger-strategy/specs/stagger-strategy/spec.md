## ADDED Requirements

### Requirement: Expiry bucket calculation
The Stagger Strategy SHALL compute expiry buckets by grouping active credits by their remaining days until expiry.

#### Scenario: Credits with different expiry days
- **WHEN** credits expire in 3, 5, and 5 days
- **THEN** bucket[3] SHALL contain the first credit's amount and bucket[5] SHALL contain the sum of the other two

### Requirement: Best period selection
The Stagger Strategy SHALL select the period within config bounds that targets the day with the least expiry amount.

#### Scenario: Gap in expiry distribution
- **WHEN** credits expire in days 3, 4, 6, 7 and config period range is 2-10
- **THEN** the strategy SHALL select period 5 (the gap day with zero expiry)

#### Scenario: Multiple empty days
- **WHEN** multiple days within period range have zero expiry amount
- **THEN** the strategy SHALL select the middle day among the emptiest days

### Requirement: No credits — default period
The Stagger Strategy SHALL use the midpoint of period range when there are no active credits.

#### Scenario: No active credits
- **WHEN** there are no active credits and period range is 2-30
- **THEN** the period SHALL be 16 (midpoint of 2-30)

### Requirement: Concentration detection
The Stagger Strategy SHALL report concentration level when the largest expiry bucket exceeds 50% of total credit amount.

#### Scenario: Concentrated expiry
- **WHEN** 80% of total credit amount expires on the same day
- **THEN** the reason SHALL be "stagger:concentrated"

#### Scenario: Well distributed
- **WHEN** no single day exceeds 50% of total credit amount
- **THEN** the reason SHALL be "stagger:balanced"

### Requirement: Flash freeze protection
#### Scenario: Flash freeze active
- **WHEN** the market snapshot has FlashFreeze = true
- **THEN** the system SHALL return an empty DecisionResult with reason "flash_freeze"

### Requirement: Insufficient balance handling
#### Scenario: Balance below minimum
- **WHEN** the available balance is less than 50 USD
- **THEN** the system SHALL return an empty DecisionResult with reason "insufficient_balance"
