## ADDED Requirements

### Requirement: Exclude degraded signals from MDC calculation
The MDC aggregator SHALL exclude signals in Degraded state from the weighted sum. Remaining healthy/warning signals SHALL have their weights renormalized to sum to 100%.

#### Scenario: One signal degraded, others healthy
- **WHEN** CrossCurrency signal is Degraded and all other signals are Healthy
- **THEN** CrossCurrency SHALL be excluded and remaining 5 signals' weights SHALL be renormalized proportionally

#### Scenario: All signals healthy
- **WHEN** all 6 signals are Healthy
- **THEN** MDC calculation SHALL use standard base weights (no change from current behavior)

#### Scenario: No health data provided
- **WHEN** health map is nil or empty
- **THEN** MDC calculation SHALL use standard base weights (backward compatible)

### Requirement: Liquidation failure fallback
When the Liquidation Cascade signal is Degraded, the Book Consumption signal weight SHALL increase to 35% (from base 25%). Other signals SHALL redistribute the remaining weight proportionally.

#### Scenario: Liquidation signal degraded
- **WHEN** LiquidationCascade signal is Degraded
- **THEN** BookConsumption weight SHALL be set to 35% and remaining signals SHALL share the rest proportionally

### Requirement: Order Book failure triggers FRR-only mode
When the Book Consumption signal (which depends on Order Book data) is Degraded, the system SHALL enter FRR-only mode by forcing MDC score to 0.0 and marking the snapshot as degraded.

#### Scenario: Order Book signal degraded
- **WHEN** BookConsumption signal is Degraded
- **THEN** MDC score SHALL be forced to 0.0
- **AND** the MarketSnapshot SHALL be marked with DegradedMode=true

### Requirement: Trade flow failure fallback
When the Momentum (VWAP) signal is Degraded, the system SHALL freeze VWAP contribution and redistribute its weight to MarginUsage and CrossCurrency signals.

#### Scenario: Momentum signal degraded
- **WHEN** Momentum signal is Degraded
- **THEN** Momentum SHALL be excluded and its 15% weight SHALL be redistributed to MarginUsage and CrossCurrency proportionally

### Requirement: Multiple simultaneous failures
When multiple signals are Degraded simultaneously, the system SHALL apply all applicable degradation rules and renormalize remaining weights.

#### Scenario: Two signals degraded
- **WHEN** both LiquidationCascade and CrossCurrency are Degraded
- **THEN** both SHALL be excluded and remaining signals' weights SHALL be renormalized
