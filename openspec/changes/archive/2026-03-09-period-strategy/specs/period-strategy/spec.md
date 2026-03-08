## ADDED Requirements

### Requirement: Regime-driven base period
The Period Strategy SHALL compute a base period by mapping the current market regime to a ratio within the user's configured period range (Period.Min to Period.Max).

#### Scenario: Contango regime
- **WHEN** the market regime is "contango" and config has Period.Min=2, Period.Max=30
- **THEN** the base period SHALL be `2 + 0.75 × (30 - 2) = 23` (75% of range, favoring longer lock-in)

#### Scenario: Neutral regime
- **WHEN** the market regime is "neutral" and config has Period.Min=2, Period.Max=30
- **THEN** the base period SHALL be `2 + 0.50 × (30 - 2) = 16` (50% of range)

#### Scenario: Backwardation regime
- **WHEN** the market regime is "backwardation" and config has Period.Min=2, Period.Max=30
- **THEN** the base period SHALL be `2 + 0.25 × (30 - 2) = 9` (25% of range, favoring flexibility)

#### Scenario: Crisis regime
- **WHEN** the market regime is "crisis"
- **THEN** the base period SHALL be Period.Min (shortest possible, maximum flexibility)

### Requirement: Rate-based period scaling
The Period Strategy SHALL scale the base period based on the ratio of the offered rate to FRR, adjusting by up to ±30%.

#### Scenario: Rate above FRR
- **WHEN** the offered rate is 1.5× the FRR
- **THEN** the period SHALL be scaled by `1.0 + (1.5 - 1.0) × 0.3 = 1.15` (+15%, worth locking in)

#### Scenario: Rate below FRR
- **WHEN** the offered rate is 0.8× the FRR
- **THEN** the period SHALL be scaled by `1.0 + (0.8 - 1.0) × 0.3 = 0.94` (-6%, keep short)

#### Scenario: FRR is zero
- **WHEN** the FRR is zero
- **THEN** the rate scaling step SHALL be skipped (scale factor = 1.0)

### Requirement: Volatility discount
The Period Strategy SHALL reduce the period when market volatility is high, to maintain flexibility for rate renegotiation.

#### Scenario: High volatility
- **WHEN** RegimeParams.Volatility is 0.20 (20%)
- **THEN** the volatility discount SHALL be `1.0 - (0.20 - 0.10) × 2.0 = 0.80` (20% reduction)

#### Scenario: Extreme volatility
- **WHEN** RegimeParams.Volatility is 0.40 (40%)
- **THEN** the volatility discount SHALL be capped at `1.0 - 0.20 × 2.0 = 0.60` (40% reduction max)

#### Scenario: Normal volatility
- **WHEN** RegimeParams.Volatility is 0.05 (5%)
- **THEN** no volatility discount SHALL be applied (discount = 1.0)

### Requirement: Period clamping and rounding
The final period SHALL be rounded to the nearest integer and clamped to the user's configured bounds.

#### Scenario: Period within bounds
- **WHEN** the calculated period is 15.4
- **THEN** the result SHALL be 15 (rounded) if within config bounds

#### Scenario: Period below minimum
- **WHEN** the calculated period rounds to 1 but config.Period.Min is 2
- **THEN** the result SHALL be 2

#### Scenario: Period above maximum
- **WHEN** the calculated period rounds to 35 but config.Period.Max is 30
- **THEN** the result SHALL be 30

### Requirement: Flash freeze protection
The Period Strategy SHALL produce no offers when a flash crash is detected.

#### Scenario: Flash freeze active
- **WHEN** the market snapshot has FlashFreeze = true
- **THEN** the system SHALL return an empty DecisionResult with reason "flash_freeze"

### Requirement: Insufficient balance handling
The Period Strategy SHALL produce no offers when available balance is below the minimum threshold (50 USD).

#### Scenario: Balance below minimum
- **WHEN** the available balance is less than 50 USD
- **THEN** the system SHALL return an empty DecisionResult with reason "insufficient_balance"
