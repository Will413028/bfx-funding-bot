## ADDED Requirements

### Requirement: Currency validation
The system SHALL validate that `Currency` is a supported Bitfinex funding currency.

#### Scenario: Valid currency
- **WHEN** `Currency` is "USD", "UST", or other supported currencies
- **THEN** validation SHALL pass

#### Scenario: Empty currency
- **WHEN** `Currency` is empty string
- **THEN** validation SHALL return error "currency is required"

### Requirement: Amount range validation
The system SHALL validate amount parameters.

#### Scenario: Min amount below platform minimum
- **WHEN** `Amount.Min` is less than 50
- **THEN** validation SHALL return error "minimum amount must be at least 50"

#### Scenario: Max less than Min
- **WHEN** `Amount.Max` is less than `Amount.Min`
- **THEN** validation SHALL return error "max amount must be greater than or equal to min amount"

#### Scenario: Valid amount range
- **WHEN** `Amount.Min` ≥ 50 and `Amount.Max` ≥ `Amount.Min`
- **THEN** validation SHALL pass

### Requirement: Rate range validation
The system SHALL validate daily rate parameters as decimal fractions (e.g., 0.0001 = 0.01%/day).

#### Scenario: Min rate not positive
- **WHEN** `Rate.Min` is ≤ 0
- **THEN** validation SHALL return error "min rate must be positive"

#### Scenario: Max rate less than min rate
- **WHEN** `Rate.Max` < `Rate.Min`
- **THEN** validation SHALL return error "max rate must be greater than or equal to min rate"

#### Scenario: Max rate exceeds platform limit
- **WHEN** `Rate.Max` exceeds the reasonable daily rate limit (e.g., > 0.01 which is 1%/day = 365%/year)
- **THEN** validation SHALL return error "max rate exceeds platform limit"

#### Scenario: Valid rate range
- **WHEN** `Rate.Min` > 0 and `Rate.Max` ≥ `Rate.Min` and `Rate.Max` ≤ platform limit
- **THEN** validation SHALL pass

### Requirement: Period range validation
The system SHALL validate lending period (days) parameters. Bitfinex allows 2-120 days.

#### Scenario: Min period below platform minimum
- **WHEN** `Period.Min` < 2
- **THEN** validation SHALL return error "min period must be at least 2 days"

#### Scenario: Max period exceeds platform maximum
- **WHEN** `Period.Max` > 120
- **THEN** validation SHALL return error "max period must not exceed 120 days"

#### Scenario: Max period less than min period
- **WHEN** `Period.Max` < `Period.Min`
- **THEN** validation SHALL return error "max period must be greater than or equal to min period"

#### Scenario: Valid period range
- **WHEN** `Period.Min` ≥ 2 and `Period.Max` ≤ 120 and `Period.Max` ≥ `Period.Min`
- **THEN** validation SHALL pass

### Requirement: Multiple validation errors
The system SHALL collect all validation errors and return them together, not stop at the first error.

#### Scenario: Multiple invalid fields
- **WHEN** both `Amount.Min` < 50 and `Period.Max` > 120
- **THEN** validation SHALL return errors for both fields
