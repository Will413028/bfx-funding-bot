## ADDED Requirements

### Requirement: Weekday — no premium
The Weekend Strategy SHALL not adjust the rate on Monday through Thursday.

#### Scenario: Wednesday
- **WHEN** the timestamp is Wednesday 14:00 UTC and base rate is 0.0003
- **THEN** the rate SHALL remain 0.0003

### Requirement: Friday evening premium
The Weekend Strategy SHALL apply a +2% rate premium on Friday from 18:00 UTC onward.

#### Scenario: Friday evening
- **WHEN** the timestamp is Friday 20:00 UTC and base rate is 0.0003
- **THEN** the rate SHALL be 0.0003 × 1.02

#### Scenario: Friday morning
- **WHEN** the timestamp is Friday 10:00 UTC and base rate is 0.0003
- **THEN** the rate SHALL remain 0.0003 (before 18:00)

### Requirement: Saturday peak premium
The Weekend Strategy SHALL apply a +5% rate premium on Saturday.

#### Scenario: Saturday
- **WHEN** the timestamp is Saturday 12:00 UTC and base rate is 0.0003
- **THEN** the rate SHALL be 0.0003 × 1.05

### Requirement: Sunday declining premium
The Weekend Strategy SHALL apply a +3% rate premium on Sunday.

#### Scenario: Sunday
- **WHEN** the timestamp is Sunday 15:00 UTC and base rate is 0.0003
- **THEN** the rate SHALL be 0.0003 × 1.03

### Requirement: Rate clamping
The Weekend Strategy SHALL clamp the adjusted rate within config rate bounds.

#### Scenario: Premium exceeds max
- **WHEN** base rate is 0.0049 and Saturday premium pushes to 0.005145
- **THEN** the rate SHALL be clamped to config.Rate.Max

### Requirement: Flash freeze protection
#### Scenario: Flash freeze active
- **WHEN** the market snapshot has FlashFreeze = true
- **THEN** the system SHALL return an empty DecisionResult with reason "flash_freeze"

### Requirement: Insufficient balance handling
#### Scenario: Balance below minimum
- **WHEN** the available balance is less than 50 USD
- **THEN** the system SHALL return an empty DecisionResult with reason "insufficient_balance"
