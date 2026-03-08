## ADDED Requirements

### Requirement: Single wall detection
The system SHALL identify individual book entries whose amount exceeds a configurable percentage (default 5%) of total same-side depth as wall positions.

#### Scenario: Large single offer
- **WHEN** an offer entry has amount 5000 and total offer depth is 50000
- **THEN** the entry SHALL be detected as a wall with WallType "single" (5000/50000 = 10% > 5%)

#### Scenario: Below threshold
- **WHEN** an offer entry has amount 2000 and total offer depth is 50000
- **THEN** the entry SHALL NOT be detected as a wall (2000/50000 = 4% < 5%)

### Requirement: Distributed wall detection
The system SHALL detect clusters of adjacent-rate entries whose combined amount exceeds the wall threshold. Entries are considered adjacent when their rate difference is less than the current spread multiplied by a configurable factor (default 2.0).

#### Scenario: Clustered small entries forming a wall
- **WHEN** three offer entries at rates 0.00100, 0.00101, 0.00102 have amounts [2000, 2500, 2000] and total offer depth is 50000 and spread is 0.00005
- **THEN** the cluster (total 6500, 13% of depth) SHALL be detected as a wall with WallType "distributed"

#### Scenario: Non-adjacent entries not clustered
- **WHEN** two offer entries at rates 0.00100 and 0.00200 have amounts [3000, 3000]
- **THEN** they SHALL NOT be clustered together (rate gap 0.001 > spread × 2)

### Requirement: Wall position output
Each detected wall SHALL include: rate (or average rate for distributed), total amount, side ("offer"/"bid"), entry count, and WallType ("single"/"distributed").

#### Scenario: Distributed wall output
- **WHEN** a distributed wall is detected from 3 entries at rates 0.001, 0.00101, 0.00102
- **THEN** the output SHALL have rate = average of the 3 rates, amount = sum, entry count = 3, WallType = "distributed"
