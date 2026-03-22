## ADDED Requirements

### Requirement: Mean reversion probability calculation

The system SHALL compute P(higher_rate) using the Ornstein-Uhlenbeck mean reversion model:
`P(higher) = Φ((EMA_7d - current_rate) / (σ × √t))`

#### Scenario: Rate far below mean
- **WHEN** current rate is 0.0001 and EMA_7d is 0.0003 with low volatility
- **THEN** P(higher) SHALL be close to 1.0 (very likely rates will rise)

#### Scenario: Rate at mean
- **WHEN** current rate equals EMA_7d
- **THEN** P(higher) SHALL be approximately 0.5

#### Scenario: Zero volatility
- **WHEN** rate volatility is 0
- **THEN** P(higher) SHALL return 0.5 (no information)

### Requirement: P(higher) influences floor tolerance

When P(higher) > 0.7, the composite pipeline SHALL accept a higher floor rate (willing to wait). When P(higher) < 0.3, it SHALL reduce floor urgency (deploy immediately).

#### Scenario: High probability of rate increase
- **WHEN** P(higher) is 0.85
- **THEN** urgency discount SHALL be reduced (less pressure to lower floor)
