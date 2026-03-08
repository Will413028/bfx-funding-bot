## ADDED Requirements

### Requirement: Residual offer detection
The Partial Strategy SHALL recommend cancelling active offers whose amount is below 50 USD (minBalance).

#### Scenario: Small residual detected
- **WHEN** an active offer has amount 30 USD
- **THEN** the offer SHALL be added to Cancels

#### Scenario: Normal offer not cancelled
- **WHEN** an active offer has amount 500 USD
- **THEN** the offer SHALL NOT be added to Cancels

### Requirement: Fill proxy calculation
The Partial Strategy SHALL compute fillProxy as 1 - (avgOfferSize / config.Amount.Max), where avgOfferSize is the average amount of active offers (excluding residuals).

#### Scenario: Calculate fill proxy
- **WHEN** active offers have amounts [3000, 5000, 2000] and config.Amount.Max is 10000
- **THEN** avgOfferSize SHALL be 3333.33 and fillProxy SHALL be approximately 0.667

#### Scenario: No active offers
- **WHEN** there are no active offers
- **THEN** fillProxy SHALL be 0

### Requirement: Low activity — amount reduction
The Partial Strategy SHALL reduce new offer amount to 80% when fillProxy is below 0.3.

#### Scenario: Low fill proxy
- **WHEN** fillProxy is 0.1 and base amount is 5000
- **THEN** the new offer amount SHALL be 4000 (5000 × 0.80)

### Requirement: Normal activity — no adjustment
The Partial Strategy SHALL not adjust amount when fillProxy is between 0.3 and 0.7.

#### Scenario: Normal fill proxy
- **WHEN** fillProxy is 0.5 and base amount is 5000
- **THEN** the new offer amount SHALL remain 5000

### Requirement: High activity — amount increase
The Partial Strategy SHALL increase new offer amount to 120% when fillProxy exceeds 0.7, capped at config.Amount.Max.

#### Scenario: High fill proxy
- **WHEN** fillProxy is 0.8 and base amount is 5000
- **THEN** the new offer amount SHALL be 6000 (5000 × 1.20)

#### Scenario: High fill proxy capped
- **WHEN** fillProxy is 0.9 and base amount is 9000 with config.Amount.Max 10000
- **THEN** the new offer amount SHALL be 10000 (9000 × 1.20 = 10800, capped at 10000)

### Requirement: Flash freeze protection
#### Scenario: Flash freeze active
- **WHEN** the market snapshot has FlashFreeze = true
- **THEN** the system SHALL return an empty DecisionResult with reason "flash_freeze"

### Requirement: Insufficient balance handling
#### Scenario: Balance below minimum
- **WHEN** the available balance is less than 50 USD
- **THEN** the system SHALL return an empty DecisionResult with reason "insufficient_balance"
