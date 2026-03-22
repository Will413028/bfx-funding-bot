## ADDED Requirements

### Requirement: Smart wall positioning

When a large offer wall is detected near the computed rate, the system SHALL position the offer at `wallRate - minTickSize` instead of applying a fixed discount. This queues the offer just ahead of the wall for faster fill.

#### Scenario: Wall within proximity
- **WHEN** computed rate is 0.00039 and an offer wall exists at 0.0004
- **THEN** rate SHALL be adjusted to `0.0004 - 0.00000001` (wall rate minus 1 tick)

#### Scenario: No wall nearby
- **WHEN** no offer wall is within 10% of the computed rate
- **THEN** rate SHALL remain unchanged

### Requirement: Sigmoid queue discount

Queue position discount SHALL use a smoothstep sigmoid curve instead of discrete thresholds, providing continuous adjustment across the full queue depth range.

Formula: `discount = 1.0 - maxDiscount × smoothstep(queueRatio, 0.1, 0.6)`

#### Scenario: Queue ratio at 0.35 (was between thresholds)
- **WHEN** queueRatio is 0.35 (previously between 0.20 and 0.50 thresholds)
- **THEN** discount SHALL be a smooth intermediate value (~0.985) instead of jumping from 0.98 to 0.95

#### Scenario: Queue ratio at 0.05 (below range)
- **WHEN** queueRatio is 0.05
- **THEN** discount SHALL be 1.0 (no adjustment)

#### Scenario: Queue ratio at 0.70 (above range)
- **WHEN** queueRatio is 0.70
- **THEN** discount SHALL be 0.95 (maximum discount)
