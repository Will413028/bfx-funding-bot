## ADDED Requirements

### Requirement: RegimeType enum
`domain/regime.go` SHALL define a `RegimeType` string enum for market regime classification.

#### Scenario: Regime types defined
- **WHEN** RegimeType constants are used
- **THEN** the following values SHALL be available: RegimeContango (demand > supply, rates rising), RegimeBackwardation (supply > demand, rates falling), RegimeNeutral (balanced), RegimeCrisis (extreme volatility / flash crash)

### Requirement: RegimeParams type
`domain/regime.go` SHALL define a `RegimeParams` struct holding parameters that describe the current market regime.

#### Scenario: RegimeParams fields
- **WHEN** a RegimeParams is created
- **THEN** it SHALL contain: Volatility (float64), TrendStrength (float64 -1 to +1), DemandSupplyRatio (float64), Duration (time.Duration, how long current regime has lasted)

### Requirement: No external dependencies
All types in `domain/regime.go` SHALL only import Go standard library packages.

#### Scenario: Import check
- **WHEN** `domain/regime.go` is compiled
- **THEN** it SHALL NOT import any non-standard-library packages
