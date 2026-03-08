## ADDED Requirements

### Requirement: Execute cancellations before placements
The OfferExecutor SHALL execute all cancellations before placing new offers.

#### Scenario: Cancel then place
- **WHEN** DecisionResult contains 2 cancels and 3 offers
- **THEN** all cancels SHALL be executed first, then all offers

### Requirement: Record successful operations
The OfferExecutor SHALL create an ExecutionRecord for each successful API operation.

#### Scenario: Successful placement
- **WHEN** a funding offer is successfully placed
- **THEN** an ExecutionRecord with action "place" and status "success" SHALL be created

#### Scenario: Successful cancellation
- **WHEN** a funding offer is successfully cancelled
- **THEN** an ExecutionRecord with action "cancel" and status "success" SHALL be created

### Requirement: Record failed operations
The OfferExecutor SHALL create an ExecutionRecord for each failed API operation and continue processing.

#### Scenario: Failed placement
- **WHEN** a funding offer placement fails
- **THEN** an ExecutionRecord with action "place", status "error", and the error message SHALL be created
- **AND** the executor SHALL continue with remaining offers

#### Scenario: Failed cancellation
- **WHEN** a funding offer cancellation fails
- **THEN** an ExecutionRecord with action "cancel", status "error", and the error message SHALL be created
- **AND** the executor SHALL continue with remaining cancels

### Requirement: API key resolution
The OfferExecutor SHALL fetch and decrypt the user's API key before executing operations.

#### Scenario: API key not found
- **WHEN** the user has no API key configured
- **THEN** the executor SHALL return an error without executing any operations

### Requirement: Execution summary
The OfferExecutor SHALL return an ExecutionSummary with counts of successful and failed operations.

#### Scenario: Mixed results
- **WHEN** 2 cancels succeed, 1 cancel fails, 3 offers succeed, 1 offer fails
- **THEN** the summary SHALL report CancelOK=2, CancelFail=1, PlaceOK=3, PlaceFail=1

### Requirement: Empty decision handling
The OfferExecutor SHALL return an empty summary without errors when DecisionResult has no offers and no cancels.

#### Scenario: No operations
- **WHEN** DecisionResult has empty Offers and empty Cancels
- **THEN** the executor SHALL return a zero-value summary with no error
