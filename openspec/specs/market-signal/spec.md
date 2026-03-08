## ADDED Requirements

### Requirement: SignalType enum
`domain/signal.go` SHALL define a `SignalType` string enum for the 7 market signal sources.

#### Scenario: Signal types defined
- **WHEN** SignalType constants are used
- **THEN** the following values SHALL be available: SignalMDC, SignalBookConsumption, SignalLiquidationCascade, SignalMomentum, SignalMarginUsage, SignalCrossCurrency, SignalIntraday

### Requirement: SignalValue type
`domain/signal.go` SHALL define a `SignalValue` struct representing a single signal computation result.

#### Scenario: SignalValue fields
- **WHEN** a SignalValue is created
- **THEN** it SHALL contain: Type (SignalType), Value (float64 normalized -1 to +1), Confidence (float64 0 to 1), Timestamp (time.Time)

### Requirement: MDCResult type
`domain/signal.go` SHALL define an `MDCResult` struct representing the Market Demand Curve computation result.

#### Scenario: MDCResult fields
- **WHEN** an MDCResult is created
- **THEN** it SHALL contain: Score (float64, weighted signal composite), DemandPressure (float64), SupplyPressure (float64), Timestamp (time.Time)

### Requirement: No external dependencies
All types in `domain/signal.go` SHALL only import Go standard library packages.

#### Scenario: Import check
- **WHEN** `domain/signal.go` is compiled
- **THEN** it SHALL NOT import any non-standard-library packages
