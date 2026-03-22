## ADDED Requirements

### Requirement: Rate percentile signal tracks historical rate distribution

The system SHALL maintain a 7-day rolling buffer of FRR values and compute the percentile rank of the current rate within this distribution.

#### Scenario: Current rate at 75th percentile
- **WHEN** current FRR is higher than 75% of values in the 7-day buffer
- **THEN** RatePercentile SHALL be 0.75 and the signal value SHALL be +0.5 ((0.75 - 0.5) × 2)

#### Scenario: Insufficient data
- **WHEN** fewer than 100 data points in the buffer
- **THEN** signal SHALL return zero value with zero confidence

### Requirement: Rate percentile influences deployment

The composite pipeline SHALL use RatePercentile to influence strategy:
- `> P75`: more aggressive rate + longer period (lock in high rates)
- `< P25`: shorter period or reduced deployment (wait for recovery)

#### Scenario: Rate at P80 in neutral regime
- **WHEN** RatePercentile is 0.80 and regime is neutral
- **THEN** composite SHALL increase period toward cfg.Period.Max
