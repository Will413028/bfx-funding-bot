### Requirement: Worker tracks idle capital duration

The worker SHALL track how long the user's capital has been idle (no active credits or offers filled) via a `lastLentAt` timestamp, updated each time an offer is successfully executed.

#### Scenario: Idle time computed correctly
- **WHEN** the worker builds DecisionContext and the last successful offer was 45 minutes ago
- **THEN** `DecisionContext.IdleMinutes` SHALL be 45.0

#### Scenario: First tick after startup
- **WHEN** the worker has no record of last successful offer (first run)
- **THEN** `DecisionContext.IdleMinutes` SHALL be 0.0 (no urgency discount)

### Requirement: Floor rate decreases with idle duration

The floor rate SHALL be reduced by an urgency discount that increases linearly with idle time, capped at 15%.

Formula: `urgencyDiscount = min(idleMinutes / 120, 0.15)`
Adjusted floor: `floor × (1 - urgencyDiscount)`

#### Scenario: 30 minutes idle
- **WHEN** capital has been idle for 30 minutes
- **THEN** urgency discount SHALL be 0.0375 (3.75%), floor reduced accordingly

#### Scenario: 2+ hours idle — cap at 15%
- **WHEN** capital has been idle for 150 minutes
- **THEN** urgency discount SHALL be capped at 0.15 (15%), not 0.1875
