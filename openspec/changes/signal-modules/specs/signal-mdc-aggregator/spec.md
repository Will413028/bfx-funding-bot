## ADDED Requirements

### Requirement: MDC aggregation with freshness decay
MDCAggregator SHALL compute a weighted composite score from multiple SignalValue inputs, applying exponential freshness decay to each weight.

#### Scenario: Normal aggregation
- **WHEN** Aggregate is called with fresh signal values
- **THEN** it SHALL compute `MDC = tanh(Σ(Signal_i × EffectiveWeight_i))` where `EffectiveWeight_i = BaseWeight_i × e^(-λ_i × age_seconds_i)` and renormalize weights to sum to 1.0

#### Scenario: Stale signal decay
- **WHEN** a signal has not been updated for a long time
- **THEN** its effective weight SHALL decay toward zero and other signals SHALL be upweighted proportionally

### Requirement: Liquidation hard override
MDCAggregator SHALL force MDC = +1.0 when a liquidation cascade is active.

#### Scenario: Liquidation active
- **WHEN** the liquidation signal has Value = 1.0 and Confidence = 1.0
- **THEN** MDC Score SHALL be forced to +1.0 regardless of other signals

#### Scenario: Liquidation regression
- **WHEN** the liquidation signal drops from 1.0 to a lower value
- **THEN** the hard override SHALL be released and normal aggregation SHALL resume

### Requirement: Demand and supply pressure decomposition
MDCAggregator SHALL decompose signals into demand and supply pressure components.

#### Scenario: Pressure decomposition
- **WHEN** signals are aggregated
- **THEN** MDCResult SHALL contain DemandPressure (sum of positive weighted signals) and SupplyPressure (sum of absolute negative weighted signals)

### Requirement: Cold start handling
MDCAggregator SHALL handle cases where no signals have been computed yet.

#### Scenario: No signals
- **WHEN** Aggregate is called with an empty signal slice
- **THEN** it SHALL return an MDCResult with Score=0, DemandPressure=0, SupplyPressure=0
