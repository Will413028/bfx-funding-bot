## ADDED Requirements

### Requirement: Basic lending strategy execution
The system SHALL execute a lending strategy for each active user based on their `StrategyConfig`. The strategy SHALL check available balance, manage active offers, and submit new offers when appropriate.

#### Scenario: Submit new offer when balance available
- **WHEN** the user's funding wallet has available balance >= 50 USD AND there are no active offers
- **THEN** the system SHALL submit a new funding offer with rate = config.Rate.Min, amount = min(available_balance, config.Amount.Max), period = config.Period.Min

#### Scenario: Skip when balance too low
- **WHEN** the user's funding wallet has available balance < 50 USD
- **THEN** the system SHALL skip offer submission for this user

#### Scenario: Skip when active offer exists and not stale
- **WHEN** the user has an active offer that was created less than 15 minutes ago
- **THEN** the system SHALL skip offer submission (keep existing offer)

### Requirement: Stale offer cancellation (Zombie TTL)
The system SHALL cancel active offers that have not been filled within 15 minutes (spec §7.1 simplified).

#### Scenario: Offer exceeds TTL
- **WHEN** an active offer has been pending for more than 15 minutes
- **THEN** the system SHALL cancel the offer, allowing the next tick to resubmit with potentially updated parameters

#### Scenario: Offer within TTL
- **WHEN** an active offer has been pending for less than 15 minutes
- **THEN** the system SHALL leave the offer unchanged

### Requirement: Interest reinvestment
The system SHALL automatically include newly settled interest in the lending pool (spec §7.1).

#### Scenario: Interest accumulates to lendable amount
- **WHEN** the available balance (including settled interest) reaches >= 50 USD after previous offers are filled
- **THEN** the system SHALL submit a new offer for the available amount on the next tick

### Requirement: Minimum balance safety guard
The system SHALL not submit offers when the available balance is below the minimum threshold (spec §8.2).

#### Scenario: Balance below minimum
- **WHEN** the funding wallet available balance is < 50 USD
- **THEN** the system SHALL not submit any offers and SHALL log the skip reason

### Requirement: Offer parameters from strategy config
The system SHALL use the user's `StrategyConfig` to determine offer parameters.

#### Scenario: Rate selection
- **WHEN** submitting an offer
- **THEN** the system SHALL use `config.Rate.Min` as the offer rate (conservative strategy for MVP)

#### Scenario: Amount selection
- **WHEN** submitting an offer
- **THEN** the system SHALL use `min(available_balance, config.Amount.Max)` as the offer amount

#### Scenario: Period selection
- **WHEN** submitting an offer
- **THEN** the system SHALL use `config.Period.Min` as the offer period

#### Scenario: Currency selection
- **WHEN** submitting an offer
- **THEN** the system SHALL use `config.Currency` as the offer currency (prefixed with "f" for Bitfinex symbol)
