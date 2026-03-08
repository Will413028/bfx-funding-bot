## ADDED Requirements

### Requirement: LendingWorker satisfies Worker interface
LendingWorker SHALL implement Run(ctx) error, Stop(), ReloadConfig(config), and UserID() as defined by the Pool's Worker interface.

#### Scenario: Interface satisfaction
- **WHEN** LendingWorker is created
- **THEN** it can be assigned to a variable of type Worker interface

### Requirement: Event-driven tick on snapshot
Run SHALL block and wait for MarketSnapshot on the snapshot channel. Each received snapshot triggers one tick cycle.

#### Scenario: Snapshot triggers tick
- **WHEN** a MarketSnapshot is sent on the snapshot channel
- **THEN** Worker executes one tick: fetch user data → build DecisionContext → apply strategy → execute decision

#### Scenario: No snapshot, no tick
- **WHEN** no snapshot is received and context is not cancelled
- **THEN** Worker remains idle, no tick executes

### Requirement: Tick cycle phases
Each tick SHALL: (1) call DataFetcher.FetchUserData, (2) build DecisionContext from snapshot + user data + config, (3) call Strategy.Apply, (4) call OfferExecutor.ExecuteDecision with the result.

#### Scenario: Full tick cycle
- **WHEN** snapshot received, DataFetcher returns valid data, Strategy returns offers
- **THEN** OfferExecutor.ExecuteDecision is called with the strategy result

#### Scenario: DataFetcher error
- **WHEN** DataFetcher.FetchUserData returns an error
- **THEN** tick is aborted, Worker continues to next snapshot

#### Scenario: Empty decision
- **WHEN** Strategy.Apply returns no offers and no cancels
- **THEN** OfferExecutor.ExecuteDecision is still called (it handles empty decisions gracefully)

### Requirement: Graceful shutdown via context
When the context is cancelled, Run SHALL stop processing and return.

#### Scenario: Context cancelled
- **WHEN** context is cancelled during idle wait
- **THEN** Run returns nil

### Requirement: Stop method
Stop SHALL signal the worker to shut down. It SHALL be safe to call multiple times.

#### Scenario: Stop called
- **WHEN** Stop is called
- **THEN** Run exits on next select iteration

### Requirement: Config hot-reload
ReloadConfig SHALL update the worker's config for the next tick without restarting. It SHALL be non-blocking.

#### Scenario: Config reload
- **WHEN** ReloadConfig is called with new config
- **THEN** next tick uses the new config

### Requirement: Panic recovery
If a tick panics, the Worker SHALL recover, log the error, and continue processing subsequent snapshots.

#### Scenario: Tick panics
- **WHEN** Strategy.Apply panics during a tick
- **THEN** Worker recovers and processes the next snapshot normally

### Requirement: WorkerState tracking
Worker SHALL track its state: Starting → Running → Stopping → Stopped. State SHALL be queryable via State() method.

#### Scenario: State transitions
- **WHEN** Worker is created, then Run starts, then Stop is called, then Run returns
- **THEN** states progress: Starting → Running → Stopping → Stopped
