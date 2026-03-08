## ADDED Requirements

### Requirement: Dynamic dust threshold calculation
The system SHALL compute a dust threshold based on the median amount of all book entries on each side (offer/bid), multiplied by a configurable factor (default 0.05).

#### Scenario: Adaptive threshold from median
- **WHEN** the offer side has entries with amounts [100, 500, 1000, 5000, 10000]
- **THEN** the dust threshold SHALL be median(100,500,1000,5000,10000) × 0.05 = 1000 × 0.05 = 50

#### Scenario: Empty book
- **WHEN** no book entries exist
- **THEN** the filter SHALL return an empty slice

### Requirement: Dust entry filtering
The system SHALL remove entries whose absolute amount is below the computed dust threshold, returning a filtered copy of the book entries without modifying the original.

#### Scenario: Filter small entries
- **WHEN** the dust threshold is 50 and entries include amounts [10, 30, 500, 1000]
- **THEN** the filtered result SHALL contain only entries with amounts [500, 1000]

#### Scenario: Both sides filtered independently
- **WHEN** offer side median is 1000 (threshold 50) and bid side median is 200 (threshold 10)
- **THEN** offer entries below 50 SHALL be removed AND bid entries below 10 SHALL be removed independently

### Requirement: Configurable filter options
The system SHALL accept optional configuration for the dust factor multiplier, allowing callers to override the default 0.05 factor.

#### Scenario: Custom factor
- **WHEN** factor is set to 0.10 and median is 1000
- **THEN** the dust threshold SHALL be 100
