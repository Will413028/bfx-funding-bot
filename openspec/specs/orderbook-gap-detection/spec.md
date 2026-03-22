### Requirement: Detect rate gaps in order book

The system SHALL scan the ask side of the order book for rate gaps — ranges where no offers exist between adjacent entries.

#### Scenario: Gap found between two entries
- **WHEN** ask entries exist at rates 0.0003 and 0.0005 with nothing between
- **THEN** a RateGap SHALL be detected with Low=0.0003, High=0.0005, Width=0.0002

#### Scenario: No significant gaps
- **WHEN** all ask entries are within minGapWidth of their neighbors
- **THEN** no gaps SHALL be reported

### Requirement: Pricing uses gap for optimal placement

When the computed rate falls within a detected gap, the pricing module SHALL place the offer at the gap's low end (most competitive position with no competition).

#### Scenario: Target rate inside a gap
- **WHEN** computed rate is 0.00040 and a gap exists from 0.00035 to 0.00045
- **THEN** rate SHALL be adjusted to 0.00035 (gap low end — most competitive)
