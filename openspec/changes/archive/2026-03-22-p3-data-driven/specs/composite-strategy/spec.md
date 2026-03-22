## ADDED Requirements

### Requirement: Rate percentile influences period and deployment

The composite pipeline SHALL use RatePercentile from the snapshot:
- `> 0.75`: extend period toward max (lock in historically high rate)
- `< 0.25`: reduce deploymentRatio by 20% (reserve capital for better opportunity)

#### Scenario: High percentile extends period
- **WHEN** RatePercentile is 0.85 and computed period is 14
- **THEN** period SHALL be extended toward cfg.Period.Max

#### Scenario: Low percentile reduces deployment
- **WHEN** RatePercentile is 0.15
- **THEN** deploymentRatio SHALL be further reduced by 20%

### Requirement: Mean reversion modulates floor urgency

When P(higher_rate) > 0.7, the idle urgency discount (G11) SHALL be halved. When < 0.3, it SHALL be doubled.

#### Scenario: High P(higher) reduces urgency
- **WHEN** P(higher) is 0.8 and idle urgency would discount floor by 10%
- **THEN** actual discount SHALL be 5% (halved — willing to wait)

### Requirement: Gap cost adjusts period preference

Period selection SHALL subtract gap cost penalty from short-period EV, making longer periods more attractive when gap time is significant.

#### Scenario: High gap cost favors longer periods
- **WHEN** avgGapMinutes is 30 and period candidates are 2d and 14d
- **THEN** 2d period's effective return SHALL be penalized by ~1% downtime vs 14d's ~0.15%
