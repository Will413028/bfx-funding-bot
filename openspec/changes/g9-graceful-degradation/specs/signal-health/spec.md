## ADDED Requirements

### Requirement: Signal health state tracking
The system SHALL track the health state of each signal source as one of three states: Healthy, Warning, or Degraded. Health state SHALL be determined by the number of heartbeat intervals since the signal's last successful update.

- **Healthy**: Signal updated within 2 heartbeats
- **Warning**: Signal not updated for more than 2 heartbeats
- **Degraded**: Signal not updated for more than 5 heartbeats

#### Scenario: Signal is healthy when recently updated
- **WHEN** a signal was last updated 1 heartbeat ago
- **THEN** the signal health state SHALL be Healthy

#### Scenario: Signal enters warning state
- **WHEN** a signal has not been updated for 3 heartbeats
- **THEN** the signal health state SHALL be Warning

#### Scenario: Signal enters degraded state
- **WHEN** a signal has not been updated for 6 heartbeats
- **THEN** the signal health state SHALL be Degraded

#### Scenario: Signal returns to healthy after update
- **WHEN** a degraded signal receives a new data point
- **THEN** the signal health state SHALL transition to Healthy (pending recovery confirmation)

### Requirement: Per-signal last update timestamp
The system SHALL record the timestamp of the last successful data point for each of the 6 signal sources (BookConsumption, LiquidationCascade, MarginUsage, Momentum, CrossCurrency, Intraday).

#### Scenario: Timestamp updated on signal computation
- **WHEN** a signal source produces a new SignalValue
- **THEN** the tracker SHALL record the current time as that signal's last update

### Requirement: Signal health summary in MarketSnapshot
The MarketSnapshot SHALL include a signal health summary containing the health state of each signal source, enabling downstream consumers (Worker, Dashboard) to access health information.

#### Scenario: Snapshot includes health summary
- **WHEN** a MarketSnapshot is assembled
- **THEN** it SHALL contain the health state for each active signal source
