## ADDED Requirements

### Requirement: WorkerDepsFactory builds complete worker dependencies
DepsFactory SHALL create a `worker.Deps` for each user containing a Strategy, DataFetcher, OfferExecutor, and snapshot channel. The factory SHALL use the Bitfinex REST client, crypto cipher, API key repository, and execution repository to construct these dependencies.

#### Scenario: Build deps for a user
- **WHEN** `BuildWorkerDeps(userID, snapshotCh)` is called
- **THEN** returned Deps contains a non-nil Strategy, DataFetcher, OfferExecutor, and the provided SnapshotCh

### Requirement: DataFetcher retrieves user private data from Bitfinex
DataFetcher SHALL fetch the user's funding wallet balance, active offers, and active credits via the Bitfinex REST API, decrypting the API secret from the key store.

#### Scenario: Fetch user data successfully
- **WHEN** FetchUserData is called for a user with a valid API key
- **THEN** returns UserData with Available balance, ActiveOffers, and ActiveCredits

#### Scenario: Fetch user data with missing API key
- **WHEN** FetchUserData is called for a user with no API key in the store
- **THEN** returns an error

### Requirement: OfferExecutor adapter bridges execution and worker packages
An adapter SHALL convert `execution.OfferExecutor.ExecuteDecision` results into `worker.ExecutionSummary`, allowing `worker.LendingWorker` to use `execution.OfferExecutor` without direct import.

#### Scenario: Adapter delegates to execution.OfferExecutor
- **WHEN** adapter's ExecuteDecision is called
- **THEN** it delegates to execution.OfferExecutor and converts the returned ExecutionSummary

### Requirement: APIKeyService triggers Worker lifecycle on key changes
APIKeyService SHALL start a Worker when a verified API key is created (and user has a StrategyConfig), and stop the Worker when the key is deleted.

#### Scenario: Create verified API key starts Worker
- **WHEN** APIKeyService.Create succeeds with status "verified" and user has a StrategyConfig
- **THEN** WorkerManager.StartWorker is called with the user's config

#### Scenario: Create unverified API key does not start Worker
- **WHEN** APIKeyService.Create succeeds with status "unverified"
- **THEN** WorkerManager.StartWorker is NOT called

#### Scenario: Delete API key stops Worker
- **WHEN** APIKeyService.Delete succeeds
- **THEN** WorkerManager.StopWorker is called (best-effort, errors ignored)

#### Scenario: Worker start failure does not fail API key creation
- **WHEN** WorkerManager.StartWorker returns an error
- **THEN** APIKeyService.Create still returns success (key is saved)

### Requirement: ConfigService triggers hot-reload on config save
ConfigService SHALL notify the Worker of new config when a strategy config is saved.

#### Scenario: Save config reloads Worker
- **WHEN** ConfigService.Save succeeds and a Worker is running
- **THEN** ConfigReloader.ReloadConfig is called with the new config

#### Scenario: Reload failure does not fail config save
- **WHEN** ConfigReloader.ReloadConfig returns an error (e.g., no Worker running)
- **THEN** ConfigService.Save still returns success

### Requirement: fx wiring replaces engine.Engine with lending.Service
The application SHALL wire lending.Service and marketfeed.Service via fx, replacing the old engine.Engine MVP.

#### Scenario: Application starts with new engine
- **WHEN** the application starts
- **THEN** marketfeed.Service and lending.Service are started via fx lifecycle hooks

#### Scenario: Application shuts down gracefully
- **WHEN** SIGTERM is received
- **THEN** lending.Service stops all Workers, then marketfeed.Service closes WS connections, then DB/Redis close

### Requirement: MarketFeed snapshots are forwarded to lending.Service
A goroutine SHALL read from marketfeed.Snapshots() channel and call lending.Service.BroadcastSnapshot() for each snapshot received.

#### Scenario: Snapshot forwarding
- **WHEN** marketfeed.Service emits a MarketSnapshot
- **THEN** lending.Service.BroadcastSnapshot is called, delivering the snapshot to all active Workers

### Requirement: Existing users resume on restart
On application startup, the system SHALL scan the database for users with verified API keys and valid strategy configs, and start a Worker for each.

#### Scenario: Restart with existing users
- **WHEN** the application starts and 3 users have verified keys + configs
- **THEN** 3 Workers are started automatically

#### Scenario: Restart with no users
- **WHEN** the application starts and no users have verified keys
- **THEN** no Workers are started, engine runs idle

### Requirement: engine/ MVP package is removed
The old engine/ package SHALL be removed entirely. No code SHALL reference engine.Engine or engine.NewEngine.

#### Scenario: engine/ package deleted
- **WHEN** the integration is complete
- **THEN** `internal/engine/` directory does not exist and `go build ./...` succeeds
