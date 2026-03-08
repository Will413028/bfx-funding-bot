package domain

import "time"

// RawMarketData holds the raw market data from a single point in time.
// This is the input to signal computation modules.
type RawMarketData struct {
	Symbol       string
	Ticker       *FundingTicker
	Book         []BookEntry
	RecentTrades []FundingTradeRecord
	Timestamp    time.Time
}

// FundingTicker represents a funding ticker snapshot (domain layer).
// Mirrors bitfinex.FundingTicker but lives in domain to avoid import dependency.
type FundingTicker struct {
	FRR              float64 // Flash Return Rate (daily)
	Bid              float64
	BidPeriod        int
	BidSize          float64
	Ask              float64
	AskPeriod        int
	AskSize          float64
	DailyChange      float64
	DailyChangePerc  float64
	LastPrice        float64
	Volume           float64
	High             float64
	Low              float64
	FRRAmountAvail   float64
}

// BookEntry represents a single order book entry (domain layer).
type BookEntry struct {
	Rate   float64
	Period int
	Count  int
	Amount float64 // >0 offer (lending), <0 bid (borrowing)
}

// FundingTradeRecord represents a single funding trade (domain layer).
type FundingTradeRecord struct {
	ID     int64
	Amount float64 // >0 lend, <0 borrow
	Rate   float64
	Period int
	MTS    time.Time
}

// OrderBookSummary summarizes the current order book state.
type OrderBookSummary struct {
	BestBid    float64 // highest bid rate
	BestAsk    float64 // lowest ask rate
	MidRate    float64 // (BestBid + BestAsk) / 2
	Spread     float64 // BestAsk - BestBid
	BidDepth   float64 // total bid amount
	AskDepth   float64 // total ask (offer) amount
	EntryCount int     // number of book entries
}

// WallType identifies the kind of detected wall.
type WallType string

const (
	WallSingle      WallType = "single"
	WallDistributed WallType = "distributed"
)

// WallPosition represents a detected large order wall in the book.
type WallPosition struct {
	Rate       float64
	Amount     float64
	Side       string   // "offer" or "bid"
	EntryCount int      // number of orders at this price level
	Type       WallType // "single" or "distributed"
}

// OrderBookAnalysis aggregates all order book analysis results.
type OrderBookAnalysis struct {
	Summary             OrderBookSummary
	Walls               []WallPosition
	HiddenRatio         float64
	CompetitorActivity  float64 // 0-1 composite score
	DustFilteredEntries int     // number of entries removed by dust filter
}

// MarketSnapshot represents the fully aggregated market state at a point in time.
// This is the output of the Market Feed Service (Phase C1) and the input to
// strategy decision modules (Phase D).
type MarketSnapshot struct {
	Symbol        string
	FRR           float64 // Flash Return Rate (daily) from ticker
	MDC           MDCResult
	Regime        RegimeType
	RegimeParams  RegimeParams
	Signals       []SignalValue
	OrderBook     OrderBookSummary
	WallPositions      []WallPosition
	HiddenRatio        float64 // estimated hidden order ratio
	CompetitorActivity float64 // 0-1 composite competitor activity score
	FlashFreeze        bool    // true if flash crash detected, trading paused
	Timestamp     time.Time
}
