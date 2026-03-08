## ADDED Requirements

### Requirement: SignalSource interface
`lending/marketfeed/interfaces.go` SHALL define a `SignalSource` interface with `Name() string` and `Compute(data *domain.RawMarketData) domain.SignalValue`.

#### Scenario: Interface contract
- **WHEN** a signal module implements SignalSource
- **THEN** it SHALL return a SignalValue with the appropriate SignalType, normalized Value (-1 to +1), and Confidence (0 to 1)

### Requirement: Service lifecycle
Market Feed Service SHALL support Start and Stop lifecycle methods.

#### Scenario: Start
- **WHEN** Start is called with a context and symbol list
- **THEN** the service SHALL connect the WSClient, subscribe to ticker/book/trades for each symbol, and begin the snapshot assembly loop

#### Scenario: Stop
- **WHEN** Stop is called or the context is cancelled
- **THEN** the service SHALL stop the snapshot loop, close the WSClient, and close the output channel

### Requirement: WebSocket data accumulation
The service SHALL accumulate latest market data from WSClient callbacks.

#### Scenario: Ticker update
- **WHEN** a new ticker is received via OnTicker callback
- **THEN** the service SHALL store it as the latest ticker for that symbol

#### Scenario: Book snapshot
- **WHEN** a book snapshot is received
- **THEN** the service SHALL replace the entire book state for that symbol

#### Scenario: Book update
- **WHEN** a book update is received with COUNT > 0
- **THEN** the service SHALL add or update the entry at that rate/period

#### Scenario: Book deletion
- **WHEN** a book update is received with COUNT = 0
- **THEN** the service SHALL remove the entry at that rate

#### Scenario: Trade executed
- **WHEN** a trade is received
- **THEN** the service SHALL append it to the recent trades buffer (keeping last 5 minutes)

### Requirement: Periodic snapshot assembly
The service SHALL assemble and broadcast a MarketSnapshot at a configurable interval (default 3 seconds).

#### Scenario: Snapshot tick
- **WHEN** the snapshot interval elapses and data is available
- **THEN** the service SHALL build RawMarketData, run signal sources, assemble MarketSnapshot, and send it to the output channel

#### Scenario: No data available
- **WHEN** the snapshot interval elapses but no ticker has been received yet
- **THEN** the service SHALL skip the snapshot and wait for the next tick

### Requirement: Snapshot output channel
The service SHALL provide a read-only channel for consumers to receive MarketSnapshot.

#### Scenario: Snapshots method
- **WHEN** Snapshots() is called
- **THEN** it SHALL return a `<-chan *domain.MarketSnapshot` that receives each assembled snapshot
