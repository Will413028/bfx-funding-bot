## ADDED Requirements

### Requirement: Engine lifecycle management
The system SHALL provide an `engine.Engine` struct that manages the lending engine lifecycle via fx.Lifecycle hooks.

#### Scenario: Engine starts with application
- **WHEN** the application starts
- **THEN** the engine SHALL begin its periodic tick loop

#### Scenario: Engine stops gracefully
- **WHEN** the application receives a shutdown signal
- **THEN** the engine SHALL cancel its context, wait for the current tick to complete, and stop cleanly

### Requirement: Periodic tick loop
The system SHALL execute a lending cycle every 3 minutes using a `time.Ticker`.

#### Scenario: Tick interval
- **WHEN** the engine is running
- **THEN** it SHALL execute a tick every 3 minutes

#### Scenario: Tick processes all active users
- **WHEN** a tick executes
- **THEN** the engine SHALL query all users with verified API keys and strategy configs, and process each user serially

#### Scenario: Single user failure does not affect others
- **WHEN** processing a user fails (API error, invalid key, etc.)
- **THEN** the engine SHALL log the error and continue processing the next user

### Requirement: Active user discovery
The system SHALL discover active users by querying API keys with `exchange_status = "verified"` and matching them with existing strategy configs.

#### Scenario: User has verified key and config
- **WHEN** a user has a verified API key AND a strategy config
- **THEN** the engine SHALL process that user in the current tick

#### Scenario: User has no config
- **WHEN** a user has a verified API key but no strategy config
- **THEN** the engine SHALL skip that user

#### Scenario: User has unverified key
- **WHEN** a user's API key has `exchange_status = "unverified"`
- **THEN** the engine SHALL skip that user

### Requirement: Panic recovery
The engine SHALL recover from panics during user processing to prevent the entire engine from crashing.

#### Scenario: Panic during user processing
- **WHEN** a panic occurs while processing a user
- **THEN** the engine SHALL recover, log the panic, and continue with the next user
