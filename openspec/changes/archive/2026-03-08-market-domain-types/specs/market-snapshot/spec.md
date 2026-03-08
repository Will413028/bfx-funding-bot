## ADDED Requirements

### Requirement: RawMarketData type
`domain/snapshot.go` SHALL define a `RawMarketData` struct containing the raw market data from a single point in time for a specific symbol.

#### Scenario: RawMarketData fields
- **WHEN** a RawMarketData is created
- **THEN** it SHALL contain: Symbol (string), Ticker (FundingTicker pointer), Book (slice of BookEntry), RecentTrades (slice of FundingTrade), Timestamp (time.Time)

### Requirement: OrderBookSummary type
`domain/snapshot.go` SHALL define an `OrderBookSummary` struct summarizing the order book state.

#### Scenario: OrderBookSummary fields
- **WHEN** an OrderBookSummary is created
- **THEN** it SHALL contain: BestBid, BestAsk, MidRate, Spread (all float64), BidDepth, AskDepth (float64 total amounts), EntryCount (int)

### Requirement: WallPosition type
`domain/snapshot.go` SHALL define a `WallPosition` struct representing a detected large order wall in the book.

#### Scenario: WallPosition fields
- **WHEN** a WallPosition is created
- **THEN** it SHALL contain: Rate (float64), Amount (float64), Side (string: "offer" or "bid"), EntryCount (int)

### Requirement: MarketSnapshot type
`domain/snapshot.go` SHALL define a `MarketSnapshot` struct representing the aggregated market state at a point in time.

#### Scenario: MarketSnapshot fields
- **WHEN** a MarketSnapshot is created
- **THEN** it SHALL contain: Symbol (string), MDC (MDCResult), Regime (RegimeType), RegimeParams (RegimeParams), Signals (slice of SignalValue), OrderBook (OrderBookSummary), WallPositions (slice of WallPosition), HiddenRatio (float64), FlashFreeze (bool), Timestamp (time.Time)

### Requirement: No external dependencies
All types in `domain/snapshot.go` SHALL only import Go standard library packages.

#### Scenario: Import check
- **WHEN** `domain/snapshot.go` is compiled
- **THEN** it SHALL NOT import any non-standard-library packages
