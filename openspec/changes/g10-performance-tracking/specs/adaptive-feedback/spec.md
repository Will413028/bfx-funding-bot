## ADDED Requirements

### Requirement: Per-module alpha aggregation
The system SHALL aggregate alpha by strategy tag over a 7-day window. For each tag, compute: mean alpha, execution count, and alpha variance.

#### Scenario: Tag with positive mean alpha
- **WHEN** strategy tag "weekend" has 10 executions with mean alpha +0.00015
- **THEN** the aggregation SHALL report tag="weekend", meanAlpha=0.00015, count=10

#### Scenario: Tag with negative mean alpha
- **WHEN** strategy tag "hidden" has 5 executions with mean alpha -0.00005
- **THEN** the aggregation SHALL report tag="hidden", meanAlpha=-0.00005, count=5

### Requirement: Adaptive adjustment generation
The system SHALL generate parameter adjustment recommendations based on 7-day per-tag alpha analysis. Positive alpha → increase weight by 5-10%. Negative alpha → decrease weight by 5-10%.

#### Scenario: Positive alpha generates increase recommendation
- **WHEN** tag "weekend" has 7-day mean alpha > 0 with low variance
- **THEN** an adjustment SHALL recommend increasing weekend premium by 5-10%

#### Scenario: Negative alpha generates decrease recommendation
- **WHEN** tag "hidden" has 7-day mean alpha < 0
- **THEN** an adjustment SHALL recommend decreasing hidden threshold by 5-10%

### Requirement: Adjustment magnitude capped at ±10%
Each adaptive adjustment SHALL NOT exceed ±10% of the parameter's current value per feedback cycle.

#### Scenario: Large alpha does not exceed cap
- **WHEN** a tag has extremely high positive alpha
- **THEN** the recommended adjustment SHALL be capped at +10%

### Requirement: Per-regime separation
Alpha aggregation SHALL separate executions by market regime. The same strategy tag MAY have different alpha in contango vs backwardation.

#### Scenario: Same tag different regime
- **WHEN** "pricing" has alpha +0.0002 in contango and -0.0001 in backwardation
- **THEN** the feedback SHALL generate separate adjustments per regime
