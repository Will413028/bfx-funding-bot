## ADDED Requirements

### Requirement: OrderBookSummary computation
`lending/marketfeed/snapshot.go` SHALL compute an OrderBookSummary from a slice of BookEntry.

#### Scenario: Normal book
- **WHEN** computeOrderBookSummary is called with book entries containing both offers (Amount>0) and bids (Amount<0)
- **THEN** it SHALL return BestBid (highest bid rate), BestAsk (lowest ask rate), MidRate, Spread, BidDepth (total abs bid amount), AskDepth (total offer amount), EntryCount

#### Scenario: Empty book
- **WHEN** computeOrderBookSummary is called with an empty slice
- **THEN** it SHALL return a zero-valued OrderBookSummary

### Requirement: WallPosition detection
`lending/marketfeed/snapshot.go` SHALL detect wall positions in the order book.

#### Scenario: Large offer wall
- **WHEN** a single offer entry's amount exceeds the wall threshold percentage (default 5%) of total offer-side depth
- **THEN** it SHALL be included in the WallPositions slice with Side="offer"

#### Scenario: No walls
- **WHEN** no single entry exceeds the threshold
- **THEN** WallPositions SHALL be an empty slice

### Requirement: MarketSnapshot assembly
`lending/marketfeed/snapshot.go` SHALL assemble a complete MarketSnapshot from RawMarketData + computed signals.

#### Scenario: Full assembly
- **WHEN** assembleSnapshot is called with RawMarketData, signal values, regime, and flash freeze status
- **THEN** it SHALL produce a MarketSnapshot with all fields populated including OrderBook summary and detected walls
