## ADDED Requirements

### Requirement: Month-end premium
The Calendar Strategy SHALL apply a rate premium in the last 3 days of each month and 1 day after.

#### Scenario: 2 days before month end
- **WHEN** the date is March 29 (2 days before March 31) and base rate is 0.0003
- **THEN** the rate SHALL be 0.0003 × 1.03

#### Scenario: Month-end day
- **WHEN** the date is March 31 and base rate is 0.0003
- **THEN** the rate SHALL be 0.0003 × 1.05

### Requirement: Quarterly expiry premium
The Calendar Strategy SHALL apply a higher rate premium around quarterly expiry (last Friday of March, June, September, December).

#### Scenario: 1 day before quarterly expiry
- **WHEN** the date is 1 day before the last Friday of March and base rate is 0.0003
- **THEN** the rate SHALL be 0.0003 × 1.07

#### Scenario: Quarterly expiry day
- **WHEN** the date is the last Friday of June and base rate is 0.0003
- **THEN** the rate SHALL be 0.0003 × 1.10

### Requirement: Higher premium takes precedence
The Calendar Strategy SHALL use the higher premium when month-end and quarterly expiry overlap.

#### Scenario: Overlap
- **WHEN** the last Friday of March falls within the last 3 days of March
- **THEN** the quarterly expiry premium SHALL be used (it is higher)

### Requirement: Period shortening before events
The Calendar Strategy SHALL limit period to daysToEvent when within 3 days of an event, with a floor of config.Period.Min.

#### Scenario: 2 days before event
- **WHEN** 2 days before month end and config.Period.Min is 2
- **THEN** period SHALL be max(2, 2) = 2

### Requirement: No event — no adjustment
The Calendar Strategy SHALL not adjust rate or period when no event is within range.

#### Scenario: Mid-month
- **WHEN** the date is March 15
- **THEN** the rate and period SHALL remain at defaults

### Requirement: Flash freeze protection
#### Scenario: Flash freeze active
- **WHEN** the market snapshot has FlashFreeze = true
- **THEN** the system SHALL return an empty DecisionResult with reason "flash_freeze"

### Requirement: Insufficient balance handling
#### Scenario: Balance below minimum
- **WHEN** the available balance is less than 50 USD
- **THEN** the system SHALL return an empty DecisionResult with reason "insufficient_balance"
