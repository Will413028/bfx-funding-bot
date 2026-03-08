## ADDED Requirements

### Requirement: Decision context type
The system SHALL define a `DecisionContext` type that aggregates all inputs needed for strategy decision-making: market snapshot, user strategy config, active offers, active credits, available balance, and currency.

#### Scenario: Strategy module receives complete context
- **WHEN** a strategy module's Apply method is called
- **THEN** it SHALL receive a `DecisionContext` containing the latest `MarketSnapshot`, the user's `StrategyConfig`, lists of active offers and credits, available balance, and currency identifier

### Requirement: Decision result type
The system SHALL define a `DecisionResult` type that represents the output of strategy decision-making: a list of offer decisions (amount, rate, period), a list of offer IDs to cancel, a list of credit IDs to renew, and a human-readable reason string.

#### Scenario: Strategy produces offer recommendations
- **WHEN** a strategy module determines that offers should be placed
- **THEN** it SHALL return a `DecisionResult` with one or more `OfferDecision` entries, each specifying amount, rate, and period

#### Scenario: Strategy produces no-op result
- **WHEN** market conditions are unfavorable (e.g., flash freeze, insufficient balance)
- **THEN** it SHALL return a `DecisionResult` with empty offers list and a descriptive reason
