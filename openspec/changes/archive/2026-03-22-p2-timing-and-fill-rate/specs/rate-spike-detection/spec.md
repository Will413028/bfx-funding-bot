## ADDED Requirements

### Requirement: Rate spike detector independent of liquidation cascade

The system SHALL detect non-liquidation rate spikes when `currentRate > EMA_1h × 2.0`, outputting a signal value [0, 1] proportional to the spike magnitude.

#### Scenario: Rate doubles vs 1hr average
- **WHEN** current FRR is 0.06% and 1hr EMA is 0.03%
- **THEN** rate spike signal SHALL fire with value ~1.0 and high confidence

#### Scenario: Normal rate fluctuation
- **WHEN** current FRR is 0.035% and 1hr EMA is 0.03%
- **THEN** rate spike signal SHALL NOT fire (ratio 1.17 < threshold 2.0)

#### Scenario: Insufficient data
- **WHEN** fewer than minimum samples collected
- **THEN** signal SHALL return zero value with zero confidence

### Requirement: Rate spike overrides period to minimum

When a rate spike is detected, the composite pipeline SHALL override the period to `cfg.Period.Min` to capture the spike with short-term lending.

#### Scenario: Spike detected during normal regime
- **WHEN** rate spike signal fires and regime is neutral
- **THEN** period SHALL be set to cfg.Period.Min regardless of regime-driven period calculation
