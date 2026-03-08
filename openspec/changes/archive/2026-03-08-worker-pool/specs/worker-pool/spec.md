## ADDED Requirements

### Requirement: Start a worker for a user
Pool.Start SHALL create and run a new Worker for the given userID. If a Worker already exists for that userID, Start SHALL return an error.

#### Scenario: Start new worker
- **WHEN** Start is called with userID="u1" and no existing worker for "u1"
- **THEN** a new Worker is created and running, Count increments by 1

#### Scenario: Start duplicate worker
- **WHEN** Start is called with userID="u1" and a worker for "u1" already exists
- **THEN** Start returns an error, existing worker is unchanged

### Requirement: Stop a worker
Pool.Stop SHALL stop the Worker for the given userID and remove it from the pool. If no Worker exists for that userID, Stop SHALL return an error.

#### Scenario: Stop existing worker
- **WHEN** Stop is called with userID="u1" and a worker exists
- **THEN** Worker is stopped, removed from pool, Count decrements by 1

#### Scenario: Stop non-existent worker
- **WHEN** Stop is called with userID="u1" and no worker exists
- **THEN** Stop returns an error

### Requirement: StopAll for graceful shutdown
Pool.StopAll SHALL stop all running Workers and wait for them to finish. It SHALL respect the provided context for timeout.

#### Scenario: StopAll with multiple workers
- **WHEN** StopAll is called with 3 running workers
- **THEN** all 3 workers are stopped, Count becomes 0

### Requirement: Reload config
Pool.Reload SHALL forward a new StrategyConfig to the Worker for the given userID. If no Worker exists, Reload SHALL return an error.

#### Scenario: Reload existing worker
- **WHEN** Reload is called with userID="u1" and new config
- **THEN** Worker receives the new config via ReloadConfig

#### Scenario: Reload non-existent worker
- **WHEN** Reload is called with userID="u1" and no worker exists
- **THEN** Reload returns an error

### Requirement: Count and Has queries
Pool.Count SHALL return the number of active Workers. Pool.Has SHALL return true if a Worker exists for the given userID.

#### Scenario: Count and Has
- **WHEN** 2 workers are running for "u1" and "u2"
- **THEN** Count=2, Has("u1")=true, Has("u2")=true, Has("u3")=false

### Requirement: Auto-cleanup on worker exit
When a Worker's Run method returns (normal or error), the pool SHALL automatically remove it from the active workers map.

#### Scenario: Worker exits normally
- **WHEN** a worker's Run returns nil
- **THEN** worker is removed from pool, Count decrements

#### Scenario: Worker exits with error
- **WHEN** a worker's Run returns an error
- **THEN** worker is removed from pool, Count decrements

### Requirement: Thread safety
All Pool operations SHALL be safe for concurrent use from multiple goroutines.

#### Scenario: Concurrent start and stop
- **WHEN** multiple goroutines call Start and Stop simultaneously
- **THEN** no data races occur, pool state remains consistent
