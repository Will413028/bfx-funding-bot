## ADDED Requirements

### Requirement: Price-following detection
The system SHALL detect price-following behavior by tracking how frequently new entries appear near the current best ask rate across consecutive book snapshots. A high follow rate (configurable threshold, default 60%) indicates automated competitor activity.

#### Scenario: High follow rate detected
- **WHEN** in 8 out of 10 recent snapshots, new entries appeared within 1 bps of the best ask
- **THEN** the follow rate SHALL be 0.8 and competitor activity score SHALL be elevated

#### Scenario: Low follow rate
- **WHEN** in 2 out of 10 recent snapshots, new entries appeared near best ask
- **THEN** the follow rate SHALL be 0.2 and competitor activity score SHALL be low

### Requirement: Round-number concentration detection
The system SHALL measure the proportion of book entries at round-number rates (integer basis points, e.g., 0.01%, 0.02%). An abnormally high proportion (above configurable threshold, default 40%) suggests automated pricing.

#### Scenario: High round-number concentration
- **WHEN** 15 out of 25 offer entries have rates at exact integer bps values
- **THEN** round-number ratio SHALL be 0.6 and this SHALL contribute to competitor activity score

#### Scenario: Organic distribution
- **WHEN** 5 out of 25 offer entries have rates at exact integer bps values
- **THEN** round-number ratio SHALL be 0.2 (below threshold, no concern)

### Requirement: Composite competitor activity score
The system SHALL output a composite `CompetitorActivity` score in [0, 1] combining price-following rate and round-number concentration. The score SHALL be the weighted average: `0.6 × followRate + 0.4 × roundNumberRatio`.

#### Scenario: Both indicators high
- **WHEN** followRate = 0.8 and roundNumberRatio = 0.6
- **THEN** CompetitorActivity SHALL be 0.6×0.8 + 0.4×0.6 = 0.72

#### Scenario: Both indicators low
- **WHEN** followRate = 0.1 and roundNumberRatio = 0.15
- **THEN** CompetitorActivity SHALL be 0.6×0.1 + 0.4×0.15 = 0.12

### Requirement: Stateful snapshot history
The system SHALL maintain a sliding window of recent book snapshots (default 10) to track entry changes and price-following patterns. The detector MUST handle cold-start by returning CompetitorActivity = 0 until at least 2 snapshots are collected.

#### Scenario: Cold start
- **WHEN** only 1 snapshot has been processed
- **THEN** CompetitorActivity SHALL be 0

#### Scenario: History cap
- **WHEN** 15 snapshots have been processed with window size 10
- **THEN** only the most recent 10 snapshots SHALL be retained
