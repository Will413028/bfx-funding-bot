### Requirement: FRR trend signal tracks short and long term EMA

The system SHALL compute a FRR trend signal using dual EMA (short=30min, long=4hr) and output a normalized value [-1, +1] indicating rate direction.

#### Scenario: Rates rising steadily
- **WHEN** short EMA (30min) is 10% above long EMA (4hr)
- **THEN** FRRTrend signal SHALL be positive (bullish), indicating rates are rising

#### Scenario: Rates falling
- **WHEN** short EMA is below long EMA
- **THEN** FRRTrend signal SHALL be negative (bearish), indicating rates are declining

#### Scenario: Insufficient data
- **WHEN** fewer than 10 data points have been received
- **THEN** FRRTrend signal SHALL return zero value with zero confidence

### Requirement: FRR trend influences pricing aggressiveness

The composite pipeline SHALL use FRRTrend to adjust the base rate: positive trend → rate increased up to +10%, negative trend → rate decreased.

#### Scenario: Strong upward trend
- **WHEN** FRRTrend is +0.8
- **THEN** base rate SHALL be multiplied by approximately 1.08 (1.0 + 0.8 × 0.1)
