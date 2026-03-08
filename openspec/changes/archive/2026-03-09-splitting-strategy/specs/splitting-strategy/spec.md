## ADDED Requirements

### Requirement: Depth-aware maximum order size
The Splitting Strategy SHALL compute a maximum per-order amount based on the order book ask depth.

#### Scenario: Normal market depth
- **WHEN** ask depth is 1,000,000 USD and depth fraction is 5%
- **THEN** the maximum per-order amount SHALL be 50,000 USD

#### Scenario: Zero ask depth
- **WHEN** ask depth is 0 (no order book data)
- **THEN** the system SHALL not split (return the original order as-is)

### Requirement: Order splitting
The Splitting Strategy SHALL split orders that exceed the maximum per-order amount into equal-sized smaller orders.

#### Scenario: Amount exceeds max
- **WHEN** the amount is 200,000 USD and max per-order is 50,000 USD
- **THEN** the system SHALL produce 4 orders of 50,000 USD each

#### Scenario: Amount within max
- **WHEN** the amount is 30,000 USD and max per-order is 50,000 USD
- **THEN** the system SHALL produce 1 order of 30,000 USD (no split)

### Requirement: Minimum split amount
Each split order SHALL have an amount of at least 50 USD. If splitting would produce orders below this minimum, the split count SHALL be reduced.

#### Scenario: Split would produce sub-minimum
- **WHEN** the amount is 200 USD and max per-order is 50 USD
- **THEN** the system SHALL produce 4 orders of 50 USD each (not 5 × 40)

#### Scenario: Amount too small to split meaningfully
- **WHEN** the amount is 80 USD and max per-order is 50 USD
- **THEN** the system SHALL produce 1 order of 80 USD (splitting to 2×40 would be below minimum)

### Requirement: Maximum split count
The Splitting Strategy SHALL produce at most 5 orders per split, even if the amount could be divided further.

#### Scenario: Very large amount
- **WHEN** the amount is 1,000,000 USD and max per-order is 50,000 USD
- **THEN** the system SHALL produce 5 orders of 200,000 USD each (capped at 5)

### Requirement: Rate and period preservation
Split orders SHALL preserve the original rate and period from the input.

#### Scenario: Split preserves parameters
- **WHEN** an order with rate=0.0003 and period=15 is split into 3 orders
- **THEN** all 3 orders SHALL have rate=0.0003 and period=15

### Requirement: Flash freeze protection
#### Scenario: Flash freeze active
- **WHEN** the market snapshot has FlashFreeze = true
- **THEN** the system SHALL return an empty DecisionResult with reason "flash_freeze"

### Requirement: Insufficient balance handling
#### Scenario: Balance below minimum
- **WHEN** the available balance is less than 50 USD
- **THEN** the system SHALL return an empty DecisionResult with reason "insufficient_balance"
