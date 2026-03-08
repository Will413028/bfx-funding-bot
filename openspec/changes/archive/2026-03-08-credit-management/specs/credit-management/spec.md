## ADDED Requirements

### Requirement: CreditManager processes expiring credits
The system SHALL scan a list of FundingCredits and identify those expiring within 1 day (remaining time ≤ 24 hours). For each expiring credit with AutoRenew=true, the system SHALL submit a new funding offer.

#### Scenario: Auto-renew expiring credit
- **WHEN** a credit has remaining time ≤ 24 hours AND AutoRenew=true
- **THEN** system submits a new funding offer with rate=max(credit.Rate, config.Rate.Min) and period=max(credit.Period, config.Period.Min) and amount=credit.Amount, and records an ExecutionRecord with action="renew" and status="success"

#### Scenario: Skip non-auto-renew credit
- **WHEN** a credit has remaining time ≤ 24 hours AND AutoRenew=false
- **THEN** system does NOT submit a new offer and increments Skipped count

#### Scenario: Skip credit not yet expiring
- **WHEN** a credit has remaining time > 24 hours (regardless of AutoRenew)
- **THEN** system does NOT submit a new offer and increments Skipped count

### Requirement: Renewal uses config-bounded parameters
The system SHALL use the greater of the credit's original rate and the config minimum rate. The system SHALL use the greater of the credit's original period and the config minimum period. The amount SHALL equal the credit's original amount.

#### Scenario: Credit rate below config minimum
- **WHEN** credit.Rate=0.0001 AND config.Rate.Min=0.0003
- **THEN** renewal offer uses rate=0.0003

#### Scenario: Credit rate above config minimum
- **WHEN** credit.Rate=0.0005 AND config.Rate.Min=0.0003
- **THEN** renewal offer uses rate=0.0005

#### Scenario: Credit period below config minimum
- **WHEN** credit.Period=2 AND config.Period.Min=3
- **THEN** renewal offer uses period=3

### Requirement: Renewal failure tolerance
The system SHALL continue processing remaining credits if a single renewal fails. Failed renewals SHALL be recorded with status="error" and the error message.

#### Scenario: One renewal fails among multiple
- **WHEN** 3 credits need renewal and the 2nd fails
- **THEN** system records error for 2nd, successfully renews 1st and 3rd, and returns RenewOK=2, RenewFail=1

### Requirement: RenewSummary reporting
ProcessCredits SHALL return a RenewSummary containing RenewOK (successful renewals), RenewFail (failed renewals), and Skipped (credits not eligible for renewal).

#### Scenario: Mixed results
- **WHEN** 5 credits: 2 expiring+AutoRenew (1 success, 1 fail), 3 not expiring
- **THEN** RenewSummary has RenewOK=1, RenewFail=1, Skipped=3

### Requirement: API key resolution
ProcessCredits SHALL resolve the user's API key and decrypt the secret before processing. If key resolution or decryption fails, the method SHALL return an error without processing any credits.

#### Scenario: API key not found
- **WHEN** KeyStore returns error for the user
- **THEN** ProcessCredits returns error and no credits are processed
