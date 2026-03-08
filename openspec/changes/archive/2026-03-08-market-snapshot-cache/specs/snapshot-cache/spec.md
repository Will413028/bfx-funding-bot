## ADDED Requirements

### Requirement: SnapshotCache interface
`repository/interfaces.go` SHALL define a `SnapshotCache` interface for caching MarketSnapshot in Redis.

#### Scenario: Interface methods
- **WHEN** SnapshotCache is used
- **THEN** it SHALL expose: Set(ctx, symbol, snapshot, ttl), Get(ctx, symbol), Delete(ctx, symbol)

### Requirement: Set snapshot with TTL
SnapshotCache.Set SHALL store a MarketSnapshot in Redis with the specified TTL.

#### Scenario: Set a snapshot
- **WHEN** Set is called with symbol "fUSD", a MarketSnapshot, and TTL 60s
- **THEN** the snapshot SHALL be JSON-serialized and stored at key `market:snapshot:fUSD` with 60s expiry

#### Scenario: Overwrite existing snapshot
- **WHEN** Set is called for a symbol that already has a cached snapshot
- **THEN** the existing value SHALL be replaced and the TTL SHALL be reset

### Requirement: Get snapshot from cache
SnapshotCache.Get SHALL retrieve a cached MarketSnapshot by symbol.

#### Scenario: Cache hit
- **WHEN** Get is called for a symbol with a valid cached snapshot
- **THEN** it SHALL return the deserialized MarketSnapshot and nil error

#### Scenario: Cache miss
- **WHEN** Get is called for a symbol with no cached snapshot (expired or never set)
- **THEN** it SHALL return nil and nil error (not an error condition)

### Requirement: Delete snapshot
SnapshotCache.Delete SHALL remove a cached snapshot by symbol.

#### Scenario: Delete existing
- **WHEN** Delete is called for a symbol with a cached snapshot
- **THEN** the cache entry SHALL be removed

#### Scenario: Delete non-existing
- **WHEN** Delete is called for a symbol with no cached snapshot
- **THEN** it SHALL return nil error (idempotent)
