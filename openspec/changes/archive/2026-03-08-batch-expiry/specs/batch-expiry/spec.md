## ADDED Requirements

### Requirement: Group credits by expiry date
BatchCredits SHALL group credits by their expiry date (UTC calendar day). Credits expiring on the same day SHALL be placed in the same group.

#### Scenario: Credits on different days
- **WHEN** 3 credits expire on day 1 and 2 credits expire on day 2
- **THEN** BatchCredits returns 2 CreditBatch entries, one per day

#### Scenario: All credits same day
- **WHEN** 5 credits all expire on the same day
- **THEN** BatchCredits returns 1 CreditBatch entry

### Requirement: Merge amounts within a batch
Each CreditBatch SHALL have Amount equal to the sum of all credit amounts in that group.

#### Scenario: Sum amounts
- **WHEN** a batch contains credits of 1000, 2000, 3000
- **THEN** CreditBatch.Amount = 6000

### Requirement: Weighted average rate
Each CreditBatch SHALL compute Rate as the amount-weighted average: Σ(credit.Amount × credit.Rate) / Σ(credit.Amount).

#### Scenario: Weighted average
- **WHEN** credit A: amount=1000, rate=0.0002 and credit B: amount=3000, rate=0.0004
- **THEN** CreditBatch.Rate = (1000×0.0002 + 3000×0.0004) / 4000 = 0.00035

### Requirement: Period uses maximum in batch
Each CreditBatch SHALL use the maximum period among credits in the group, capped at config.Period.Max.

#### Scenario: Max period
- **WHEN** credits have periods 5, 10, 15 and config.Period.Max=30
- **THEN** CreditBatch.Period = 15

#### Scenario: Max period exceeds config
- **WHEN** credits have periods 5, 10, 15 and config.Period.Max=12
- **THEN** CreditBatch.Period = 12

### Requirement: Config minimum bounds
Rate SHALL be at least config.Rate.Min. Period SHALL be at least config.Period.Min.

#### Scenario: Rate below config min
- **WHEN** weighted average rate = 0.0001 and config.Rate.Min = 0.0003
- **THEN** CreditBatch.Rate = 0.0003

#### Scenario: Period below config min
- **WHEN** max period in batch = 2 and config.Period.Min = 3
- **THEN** CreditBatch.Period = 3

### Requirement: Split oversized batches
If a batch's total amount exceeds config.Amount.Max, the system SHALL split it into multiple CreditBatch entries, each not exceeding Amount.Max. The last batch receives the remainder.

#### Scenario: Amount exceeds max
- **WHEN** batch total = 25000 and config.Amount.Max = 10000
- **THEN** 3 batches produced: 10000, 10000, 5000

#### Scenario: Amount within max
- **WHEN** batch total = 8000 and config.Amount.Max = 10000
- **THEN** 1 batch produced: 8000

### Requirement: Track original credit IDs
Each CreditBatch SHALL contain the list of original credit IDs that were merged into it. When split, each sub-batch shares the same credit IDs.

#### Scenario: IDs preserved
- **WHEN** credits 101, 102, 103 are merged into one batch
- **THEN** CreditBatch.CreditIDs = [101, 102, 103]

### Requirement: Empty input handling
BatchCredits SHALL return an empty slice when given no credits.

#### Scenario: No credits
- **WHEN** credits slice is empty
- **THEN** returns empty []CreditBatch
