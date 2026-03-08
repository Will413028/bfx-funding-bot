## ADDED Requirements

### Requirement: Intraday signal source
`lending/signal/intraday.go` SHALL implement the `SignalSource` interface (`Name() string` + `Compute(*domain.RawMarketData) domain.SignalValue`) to produce a time-based demand signal.

#### Scenario: Signal source name
- **WHEN** `Name()` is called
- **THEN** it SHALL return `"intraday"`

#### Scenario: Signal type
- **WHEN** `Compute()` returns a `SignalValue`
- **THEN** the `Type` field SHALL be `domain.SignalIntraday`

### Requirement: High-demand trading sessions
The signal SHALL detect three high-demand trading sessions based on UTC time from `RawMarketData.Timestamp`.

#### Scenario: Asia open (UTC 00:00–03:00)
- **WHEN** the timestamp falls within UTC 00:00–03:00 (fully inside session)
- **THEN** the signal value SHALL be in the range +0.3 to +0.5

#### Scenario: Europe open (UTC 07:00–09:00)
- **WHEN** the timestamp falls within UTC 07:00–09:00 (fully inside session)
- **THEN** the signal value SHALL be in the range +0.3 to +0.5

#### Scenario: US open (UTC 13:00–15:00)
- **WHEN** the timestamp falls within UTC 13:00–15:00 (fully inside session)
- **THEN** the signal value SHALL be in the range +0.3 to +0.5

#### Scenario: Outside all sessions
- **WHEN** the timestamp falls outside all three high-demand sessions and their transition zones
- **THEN** the signal value SHALL be -0.3 (low demand baseline)

### Requirement: Smooth transition at session boundaries
The signal SHALL use linear interpolation over a 30-minute transition zone at the start and end of each session, avoiding abrupt signal jumps.

#### Scenario: Entering a session
- **WHEN** the timestamp is within 30 minutes before a session start
- **THEN** the signal SHALL linearly interpolate from -0.3 to the session peak value

#### Scenario: Leaving a session
- **WHEN** the timestamp is within 30 minutes after a session end
- **THEN** the signal SHALL linearly interpolate from the session peak value to -0.3

### Requirement: Month-of-day position multiplier
The signal value SHALL be multiplied by a month-position factor before output.

#### Scenario: Month-end (day 28–31)
- **WHEN** the day of month is 28, 29, 30, or 31
- **THEN** the signal value SHALL be multiplied by 1.1

#### Scenario: Month-start (day 1–3)
- **WHEN** the day of month is 1, 2, or 3
- **THEN** the signal value SHALL be multiplied by 1.05

#### Scenario: Mid-month (day 4–27)
- **WHEN** the day of month is between 4 and 27
- **THEN** the signal value SHALL be multiplied by 1.0 (no adjustment)

### Requirement: Signal value clamping
After applying the month-position multiplier, the signal value SHALL be clamped to the range [-0.5, +0.5].

#### Scenario: Multiplier causes overflow
- **WHEN** the month-end multiplier (1.1) pushes a +0.5 signal to +0.55
- **THEN** the output SHALL be clamped to +0.5

### Requirement: Confidence output
The signal SHALL always output confidence = 1.0, as time-based signals have no uncertainty.

#### Scenario: Confidence value
- **WHEN** `Compute()` is called at any time
- **THEN** the returned `SignalValue.Confidence` SHALL be 1.0

### Requirement: Stateless computation
The intraday signal SHALL be stateless — each `Compute()` call depends only on the current `RawMarketData.Timestamp`, with no internal history buffer.

#### Scenario: No state dependency
- **WHEN** two `Compute()` calls are made with the same timestamp
- **THEN** the results SHALL be identical regardless of call order or prior calls

### Requirement: No external dependencies
`lending/signal/intraday.go` SHALL only import `domain/` and Go standard library. No I/O, no network calls.

#### Scenario: Import check
- **WHEN** the file is compiled
- **THEN** it SHALL NOT import any package outside `domain/` and Go standard library

### Requirement: MDC weight allocation
`lending/signal/mdc.go` SHALL allocate 10% weight to `domain.SignalIntraday` in `defaultWeights`.

#### Scenario: Weight distribution
- **WHEN** the MDC aggregator is initialized
- **THEN** the weights SHALL sum to 1.0 with SignalIntraday receiving 0.10

### Requirement: MDC decay configuration
`lending/signal/mdc.go` SHALL define a decay λ for `SignalIntraday`.

#### Scenario: Intraday decay rate
- **WHEN** the MDC aggregator processes an intraday signal
- **THEN** it SHALL use a decay λ of 0.002 (very slow decay, since time-based signals change slowly)

### Requirement: Registration in main.go
`cmd/server/main.go` SHALL include `signal.NewIntraday()` in the signal sources list passed to `marketfeed.NewService`.

#### Scenario: Signal source registered
- **WHEN** the market feed service starts
- **THEN** it SHALL have 6 signal sources including the intraday signal
