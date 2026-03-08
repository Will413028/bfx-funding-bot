## ADDED Requirements

### Requirement: Queue depth estimation
The Queue Strategy SHALL estimate queue depth as askDepth × (rate - bestAsk) / spread.

#### Scenario: Calculate queue depth
- **WHEN** rate is 0.00028, bestAsk is 0.00026, spread is 0.00002, and askDepth is 100,000
- **THEN** queueDepth SHALL be 100000 × (0.00028 - 0.00026) / 0.00002 = 100,000

#### Scenario: Rate at best ask
- **WHEN** rate equals bestAsk
- **THEN** queueDepth SHALL be 0 (front of queue)

### Requirement: Low queue — no adjustment
The Queue Strategy SHALL not adjust rate when queue ratio (queueDepth / askDepth) is at or below 20%.

#### Scenario: Low queue ratio
- **WHEN** queueDepth / askDepth is 0.15 (15%)
- **THEN** the rate SHALL remain unchanged

### Requirement: Moderate queue — rate discount
The Queue Strategy SHALL apply a -2% rate discount when queue ratio is between 20% and 50%.

#### Scenario: Moderate queue
- **WHEN** queueDepth / askDepth is 0.35 and base rate is 0.0003
- **THEN** the rate SHALL be adjusted to 0.0003 × 0.98

### Requirement: Deep queue — aggressive rate discount
The Queue Strategy SHALL apply a -5% rate discount when queue ratio exceeds 50%.

#### Scenario: Deep queue
- **WHEN** queueDepth / askDepth is 0.70 and base rate is 0.0003
- **THEN** the rate SHALL be adjusted to 0.0003 × 0.95

### Requirement: Stale offer cancellation
The Queue Strategy SHALL recommend cancelling active offers whose rate exceeds bestAsk + 2 × spread.

#### Scenario: Stale offer detected
- **WHEN** an active offer has rate 0.00032 and bestAsk is 0.00026 with spread 0.00002
- **THEN** the offer SHALL be added to Cancels (0.00032 > 0.00026 + 2×0.00002 = 0.00030)

#### Scenario: Offer within market range
- **WHEN** an active offer has rate 0.00028 and bestAsk is 0.00026 with spread 0.00002
- **THEN** the offer SHALL NOT be cancelled (0.00028 ≤ 0.00030)

### Requirement: No data handling
#### Scenario: Zero spread or zero ask depth
- **WHEN** spread is 0 or askDepth is 0
- **THEN** the system SHALL skip queue adjustment and return with reason "queue:no_data"

### Requirement: Flash freeze protection
#### Scenario: Flash freeze active
- **WHEN** the market snapshot has FlashFreeze = true
- **THEN** the system SHALL return an empty DecisionResult with reason "flash_freeze"

### Requirement: Insufficient balance handling
#### Scenario: Balance below minimum
- **WHEN** the available balance is less than 50 USD
- **THEN** the system SHALL return an empty DecisionResult with reason "insufficient_balance"
