## ADDED Requirements

### Requirement: Regime classification from MDC score
The system SHALL classify the market regime based on the MDC composite score using configurable thresholds (default enter=0.3, exit=0.2):
- **Contango**: score > enter threshold
- **Backwardation**: score < -enter threshold
- **Neutral**: |score| <= enter threshold
- **Crisis**: flash freeze active OR (|score| > 0.9 AND volatility > crisis volatility threshold)

#### Scenario: Contango detection
- **WHEN** MDC score is 0.5 and no flash freeze
- **THEN** regime SHALL be "contango"

#### Scenario: Backwardation detection
- **WHEN** MDC score is -0.4 and no flash freeze
- **THEN** regime SHALL be "backwardation"

#### Scenario: Neutral market
- **WHEN** MDC score is 0.1 and no flash freeze
- **THEN** regime SHALL be "neutral"

#### Scenario: Crisis from flash freeze
- **WHEN** flash freeze is active regardless of MDC score
- **THEN** regime SHALL be "crisis"

### Requirement: Hysteresis for regime transitions
The system SHALL apply hysteresis to prevent rapid regime switching. Once a regime is entered at the enter threshold, it SHALL only exit when the score crosses the exit threshold (default 0.2).

#### Scenario: Maintain regime within hysteresis band
- **WHEN** current regime is contango (entered at score 0.35) and score drops to 0.25
- **THEN** regime SHALL remain "contango" (0.25 > exit threshold 0.2)

#### Scenario: Exit regime below exit threshold
- **WHEN** current regime is contango and score drops to 0.15
- **THEN** regime SHALL transition to "neutral" (0.15 < exit threshold 0.2)

### Requirement: RegimeParams computation
The system SHALL output `RegimeParams` with each detection:
- **Volatility**: absolute value of ticker's DailyChangePerc, smoothed with EMA (alpha=0.3)
- **TrendStrength**: MDC score mapped directly to [-1, +1]
- **DemandSupplyRatio**: MDC DemandPressure / max(SupplyPressure, 0.001)
- **Duration**: time elapsed since last regime transition

#### Scenario: Params for contango regime
- **WHEN** regime is contango with MDC score 0.6, DemandPressure 0.5, SupplyPressure 0.1
- **THEN** TrendStrength SHALL be 0.6 AND DemandSupplyRatio SHALL be 5.0

#### Scenario: Duration tracking
- **WHEN** regime transitioned to contango 10 minutes ago
- **THEN** Duration SHALL be approximately 10 minutes

### Requirement: Cold start behavior
The system SHALL default to "neutral" regime with zero-value params until sufficient data is available (at least 1 MDC computation).

#### Scenario: First call
- **WHEN** RegimeDetector is called for the first time
- **THEN** regime SHALL be "neutral" and Duration SHALL be 0
