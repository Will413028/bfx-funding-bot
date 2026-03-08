## ADDED Requirements

### Requirement: Lockup cost calculation
The Lockup Strategy SHALL compute a lockup cost based on period length, base cost per day, and volatility amplification.

#### Scenario: Standard lockup cost
- **WHEN** period is 30 days, base cost is 0.000005/day, volatility is 0.10, and vol multiplier is 5.0
- **THEN** the lockup cost SHALL be `0.000005 × 30 × (1 + 5.0 × 0.10) = 0.000225`

#### Scenario: Minimal period
- **WHEN** period is 2 days with zero volatility
- **THEN** the lockup cost SHALL be `0.000005 × 2 × 1.0 = 0.00001`

### Requirement: Regime-amplified lockup cost
The Lockup Strategy SHALL amplify the lockup cost during adverse market regimes.

#### Scenario: Crisis regime
- **WHEN** the market regime is "crisis"
- **THEN** the lockup cost SHALL be multiplied by 2.0

#### Scenario: Backwardation regime
- **WHEN** the market regime is "backwardation"
- **THEN** the lockup cost SHALL be multiplied by 1.5

#### Scenario: Contango or neutral regime
- **WHEN** the market regime is "contango" or "neutral"
- **THEN** no regime amplification SHALL be applied (multiplier = 1.0)

### Requirement: Rate premium output
The Lockup Strategy SHALL output a rate that includes the lockup cost as a premium on top of the base rate.

#### Scenario: Rate with lockup premium
- **WHEN** the base rate (FRR) is 0.00025 and lockup cost is 0.000225
- **THEN** the output rate SHALL be 0.00025 + 0.000225 = 0.000475

### Requirement: Period shortening suggestion
The Lockup Strategy SHALL suggest a shorter period when the lockup cost exceeds a maximum cost ratio (default 20%) of the rate.

#### Scenario: Lockup cost exceeds threshold
- **WHEN** lockup cost / rate > 0.20
- **THEN** the output period SHALL be reduced to the maximum period where lockup cost / rate <= 0.20

#### Scenario: Lockup cost within threshold
- **WHEN** lockup cost / rate <= 0.20
- **THEN** the period SHALL remain unchanged

### Requirement: Flash freeze protection
#### Scenario: Flash freeze active
- **WHEN** the market snapshot has FlashFreeze = true
- **THEN** the system SHALL return an empty DecisionResult with reason "flash_freeze"

### Requirement: Insufficient balance handling
#### Scenario: Balance below minimum
- **WHEN** the available balance is less than 50 USD
- **THEN** the system SHALL return an empty DecisionResult with reason "insufficient_balance"
