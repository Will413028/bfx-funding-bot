## MODIFIED Requirements

### Requirement: SignalType enum
`domain/signal.go` SHALL define a `SignalType` string enum for the 7 market signal sources.

#### Scenario: Signal types defined
- **WHEN** SignalType constants are used
- **THEN** the following values SHALL be available: SignalMDC, SignalBookConsumption, SignalLiquidationCascade, SignalMomentum, SignalMarginUsage, SignalCrossCurrency, SignalIntraday
