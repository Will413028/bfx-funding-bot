## ADDED Requirements

### Requirement: Reinvest when available exceeds minimum
CheckAndReinvest SHALL submit a funding offer when available ≥ config.Amount.Min.

#### Scenario: Available above minimum
- **WHEN** available=200 and config.Amount.Min=50
- **THEN** system submits a funding offer and returns ReinvestSummary with Placed=true

#### Scenario: Available below minimum
- **WHEN** available=30 and config.Amount.Min=50
- **THEN** system does NOT submit an offer and returns ReinvestSummary with Placed=false, Reason="below_minimum"

### Requirement: Reinvest amount capped at config max
The offer amount SHALL be min(available, config.Amount.Max).

#### Scenario: Available within max
- **WHEN** available=3000 and config.Amount.Max=10000
- **THEN** offer amount = 3000

#### Scenario: Available exceeds max
- **WHEN** available=15000 and config.Amount.Max=10000
- **THEN** offer amount = 10000

### Requirement: Conservative rate and period
The offer SHALL use config.Rate.Min as rate and config.Period.Min as period.

#### Scenario: Uses config minimums
- **WHEN** config.Rate.Min=0.0002 and config.Period.Min=2
- **THEN** offer rate=0.0002 and period=2

### Requirement: Record execution
Each reinvest offer SHALL be recorded as an ExecutionRecord with action="place" and status reflecting the API result.

#### Scenario: Successful reinvest
- **WHEN** SubmitFundingOffer succeeds
- **THEN** ExecutionRecord has action="place", status="success"

#### Scenario: Failed reinvest
- **WHEN** SubmitFundingOffer fails
- **THEN** ExecutionRecord has action="place", status="error", ErrorMessage set

### Requirement: API key resolution
CheckAndReinvest SHALL resolve the user's API key before submitting. If resolution or decryption fails, return error without submitting.

#### Scenario: API key not found
- **WHEN** KeyStore returns error
- **THEN** returns error, no offer submitted

### Requirement: ReinvestSummary reporting
CheckAndReinvest SHALL return a ReinvestSummary with Placed (bool), Amount (float64), and Reason (string).

#### Scenario: Summary on success
- **WHEN** offer successfully placed with amount=3000
- **THEN** ReinvestSummary{Placed: true, Amount: 3000}

#### Scenario: Summary on skip
- **WHEN** available below minimum
- **THEN** ReinvestSummary{Placed: false, Reason: "below_minimum"}
