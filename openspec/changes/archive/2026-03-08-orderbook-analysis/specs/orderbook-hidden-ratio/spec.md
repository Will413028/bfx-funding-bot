## ADDED Requirements

### Requirement: Hidden ratio estimation from trade volume
The system SHALL estimate the hidden order ratio by comparing recent trade volume against visible book depth. The formula SHALL be: `HiddenRatio = max(0, (TradeVolume - VisibleDepth) / TradeVolume)`.

#### Scenario: Trade volume exceeds visible depth
- **WHEN** recent 5-minute trade volume is 100000 and visible offer depth is 60000
- **THEN** HiddenRatio SHALL be (100000 - 60000) / 100000 = 0.4

#### Scenario: Visible depth exceeds trade volume
- **WHEN** recent 5-minute trade volume is 30000 and visible offer depth is 60000
- **THEN** HiddenRatio SHALL be 0 (clamped, no negative values)

### Requirement: Minimum volume threshold
The system SHALL return HiddenRatio = 0 when trade volume within the window is below a configurable minimum (default 10000), as the estimate is unreliable with insufficient data.

#### Scenario: Low trade volume
- **WHEN** recent 5-minute trade volume is 5000 (below 10000 threshold)
- **THEN** HiddenRatio SHALL be 0 regardless of visible depth

### Requirement: Stateful trade volume tracking
The system SHALL maintain a sliding window (default 5 minutes) of recent trades to compute cumulative trade volume. Old trades beyond the window SHALL be pruned on each computation.

#### Scenario: Trades outside window pruned
- **WHEN** a trade occurred 6 minutes ago and the window is 5 minutes
- **THEN** that trade SHALL NOT be included in the volume calculation

#### Scenario: Accumulate trades within window
- **WHEN** 3 trades of amounts 10000, 20000, 15000 occur within the window
- **THEN** total trade volume SHALL be 45000
