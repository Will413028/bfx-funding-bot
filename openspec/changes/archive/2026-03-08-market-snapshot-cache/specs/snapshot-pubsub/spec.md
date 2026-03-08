## ADDED Requirements

### Requirement: SnapshotPubSub interface
`repository/interfaces.go` SHALL define a `SnapshotPubSub` interface for broadcasting MarketSnapshot via Redis Pub/Sub.

#### Scenario: Interface methods
- **WHEN** SnapshotPubSub is used
- **THEN** it SHALL expose: Publish(ctx, snapshot), Subscribe(ctx) returns channel, Close()

### Requirement: Publish snapshot
SnapshotPubSub.Publish SHALL broadcast a MarketSnapshot to all subscribers via Redis Pub/Sub.

#### Scenario: Publish a snapshot
- **WHEN** Publish is called with a MarketSnapshot
- **THEN** the snapshot SHALL be JSON-serialized and published to Redis channel `market:snapshot:updates`

### Requirement: Subscribe to snapshots
SnapshotPubSub.Subscribe SHALL return a Go channel that receives MarketSnapshot updates.

#### Scenario: Receive published snapshot
- **WHEN** Subscribe is called and a snapshot is published
- **THEN** the subscriber SHALL receive the deserialized MarketSnapshot on the returned channel

#### Scenario: Multiple subscribers
- **WHEN** multiple Subscribe calls are made
- **THEN** each subscriber SHALL receive all published snapshots independently

### Requirement: Close subscription
SnapshotPubSub.Close SHALL clean up the Redis subscription and close the Go channel.

#### Scenario: Close
- **WHEN** Close is called
- **THEN** the Redis subscription SHALL be terminated and the subscriber channel SHALL be closed

### Requirement: Deserialization error handling
SnapshotPubSub SHALL handle deserialization errors gracefully.

#### Scenario: Malformed message
- **WHEN** a malformed message is received on the Pub/Sub channel
- **THEN** it SHALL be logged and skipped without crashing the subscriber
