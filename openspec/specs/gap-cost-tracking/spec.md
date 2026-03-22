### Requirement: Worker tracks gap duration between credit expiry and new offer

The worker SHALL track the average time between credit expiry and new offer fill, using an EMA to smooth the measurements.

#### Scenario: Offer fills 15 minutes after credit expires
- **WHEN** a credit expired and the next offer was filled 15 minutes later
- **THEN** avgGapMinutes SHALL update toward 15.0 via EMA smoothing

#### Scenario: First tick — no gap data
- **WHEN** no previous gap measurements exist
- **THEN** AvgGapMinutes SHALL be 0 (no gap cost penalty)

### Requirement: Gap cost penalizes short periods

Period decisions SHALL include gap cost: `gapCost = avgGapMinutes / (period × 1440)`. Shorter periods have higher relative gap cost.

#### Scenario: 20 minute average gap with 2-day period
- **WHEN** avgGapMinutes is 20 and period is 2
- **THEN** gapCost SHALL be 20 / 2880 ≈ 0.0069 (0.69% downtime)

#### Scenario: 20 minute average gap with 30-day period
- **WHEN** avgGapMinutes is 20 and period is 30
- **THEN** gapCost SHALL be 20 / 43200 ≈ 0.0005 (0.05% downtime)
