## ADDED Requirements

### Requirement: Service Start and Stop lifecycle
Service.Start SHALL start the quota refill goroutine and block until ctx is cancelled. Service.Stop SHALL stop all workers and clean up resources.

#### Scenario: Start then Stop
- **WHEN** Start is called, then Stop is called
- **THEN** quota refill runs during Start, all workers stopped on Stop, no goroutine leaks

### Requirement: StartWorker integrates quota and pool
StartWorker SHALL set the user's quota based on plan, create a snapshot channel, build worker deps, and start the worker via the pool.

#### Scenario: Start a worker
- **WHEN** StartWorker("u1", config) is called
- **THEN** user quota is set, worker is running in pool, snapshot channel created

#### Scenario: Start worker with existing worker
- **WHEN** StartWorker("u1", config) is called and "u1" already has a worker
- **THEN** returns error

### Requirement: StopWorker integrates quota and pool
StopWorker SHALL stop the worker via pool, remove user quota, and close the snapshot channel.

#### Scenario: Stop a worker
- **WHEN** StopWorker("u1") is called
- **THEN** worker stopped, quota removed, snapshot channel closed

#### Scenario: Stop non-existent worker
- **WHEN** StopWorker("u1") called with no active worker
- **THEN** returns error

### Requirement: ReloadConfig delegates to pool
ReloadConfig SHALL forward the new config to pool.Reload.

#### Scenario: Reload config
- **WHEN** ReloadConfig("u1", newConfig) is called
- **THEN** pool.Reload("u1", newConfig) is called

### Requirement: BroadcastSnapshot fans out to all workers
BroadcastSnapshot SHALL send the snapshot to all active workers' snapshot channels. If a channel is full, the stale value SHALL be dropped and replaced.

#### Scenario: Broadcast to multiple workers
- **WHEN** 3 workers are active and BroadcastSnapshot is called
- **THEN** all 3 workers receive the snapshot on their channels

#### Scenario: Broadcast with slow worker
- **WHEN** a worker's snapshot channel is full
- **THEN** stale snapshot is dropped, new one is sent

### Requirement: Satisfies WorkerManager and ConfigReloader
Service SHALL have methods matching service.WorkerManager (StartWorker, StopWorker) and service.ConfigReloader (ReloadConfig) interfaces.

#### Scenario: Interface compatibility
- **WHEN** Service is instantiated
- **THEN** it satisfies both WorkerManager and ConfigReloader at compile time
