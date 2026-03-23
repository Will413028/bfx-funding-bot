## ADDED Requirements

### Requirement: Recovery confirmation period
A signal that transitions from Degraded to receiving new data SHALL NOT be immediately reintegrated into MDC calculation. The signal MUST maintain Healthy state for 2 consecutive heartbeats before reintegration.

#### Scenario: Signal recovers and passes confirmation
- **WHEN** a Degraded signal receives new data and remains Healthy for 2 consecutive heartbeats
- **THEN** the signal SHALL be reintegrated into MDC calculation with its base weight

#### Scenario: Signal recovers but becomes stale during confirmation
- **WHEN** a Degraded signal receives new data but goes stale again within 2 heartbeats
- **THEN** the signal SHALL NOT be reintegrated and the confirmation counter SHALL reset

#### Scenario: Signal that was never degraded needs no confirmation
- **WHEN** a Healthy signal receives an update
- **THEN** no confirmation period is required and the signal continues to participate normally

### Requirement: Recovering state
Signals in the recovery confirmation period SHALL be tracked as a distinct "Recovering" state. During this state, the signal SHALL be treated as Degraded for MDC calculation purposes.

#### Scenario: Signal in recovering state excluded from MDC
- **WHEN** a signal is in Recovering state (passed 0 or 1 of 2 confirmation heartbeats)
- **THEN** the signal SHALL be excluded from MDC calculation

#### Scenario: Recovery state visible in health summary
- **WHEN** a signal is in Recovering state
- **THEN** the signal health summary SHALL report the signal as Recovering
