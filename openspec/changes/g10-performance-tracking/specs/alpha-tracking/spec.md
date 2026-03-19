## ADDED Requirements

### Requirement: PerformanceRecord type
The system SHALL define a PerformanceRecord type capturing: user ID, actual rate, FRR at time, MDC score, market regime, strategy tags (string array), amount, period, alpha (computed), and timestamp.

#### Scenario: Record created after execution
- **WHEN** a lending offer is successfully placed
- **THEN** a PerformanceRecord SHALL be created with alpha = actual_rate - frr_at_time

### Requirement: Alpha calculation
The system SHALL compute alpha as the difference between the actual fill rate and the FRR baseline at the time of execution. Alpha = actual_rate - frr_at_time.

#### Scenario: Positive alpha
- **WHEN** actual_rate = 0.0005 and frr_at_time = 0.0003
- **THEN** alpha SHALL be 0.0002

#### Scenario: Negative alpha
- **WHEN** actual_rate = 0.0002 and frr_at_time = 0.0003
- **THEN** alpha SHALL be -0.0001

### Requirement: Strategy tag attribution
Each PerformanceRecord SHALL include a list of strategy tags identifying which strategy modules contributed to the decision. Tags are string identifiers (e.g., "pricing", "floor", "weekend", "hidden").

#### Scenario: Multiple tags per execution
- **WHEN** an offer is placed using pricing + weekend premium + hidden flag
- **THEN** strategy_tags SHALL contain ["pricing", "weekend", "hidden"]

### Requirement: Rolling alpha summary
The system SHALL compute rolling alpha summaries per user: 7-day average alpha, 7-day total alpha, count of executions, and per-regime breakdown.

#### Scenario: 7-day summary calculation
- **WHEN** a user has 50 executions in the past 7 days
- **THEN** the summary SHALL include mean alpha, total alpha, and count=50

### Requirement: Redis persistence with TTL
PerformanceRecords SHALL be stored in Redis with a 7-day TTL. Each record SHALL be independently keyed by user ID and timestamp.

#### Scenario: Record expires after 7 days
- **WHEN** a PerformanceRecord is stored
- **THEN** it SHALL automatically expire from Redis after 7 days
