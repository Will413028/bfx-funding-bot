package domain

import "time"

// RawMarketData holds the raw market data from a single point in time.
// This is the input to signal computation modules.
type RawMarketData struct {
	Timestamp    time.Time
	Ticker       *FundingTicker
	Symbol       string
	Book         []BookEntry
	RecentTrades []FundingTradeRecord
}

// FundingTicker represents a funding ticker snapshot (domain layer).
// Mirrors bitfinex.FundingTicker but lives in domain to avoid import dependency.
type FundingTicker struct {
	FRR             float64 // Flash Return Rate (daily)
	Bid             float64
	BidPeriod       int
	BidSize         float64
	Ask             float64
	AskPeriod       int
	AskSize         float64
	DailyChange     float64
	DailyChangePerc float64
	LastPrice       float64
	Volume          float64
	High            float64
	Low             float64
	FRRAmountAvail  float64
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
	MTS    time.Time
	ID     int64
	Amount float64
	Rate   float64
	Period int
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
	Side       string
	Type       WallType
	Rate       float64
	Amount     float64
	EntryCount int
}

// OrderBookAnalysis aggregates all order book analysis results.
type OrderBookAnalysis struct {
	Walls               []WallPosition
	Summary             OrderBookSummary
	HiddenRatio         float64
	CompetitorActivity  float64
	DustFilteredEntries int
}

// MarketSnapshot represents the fully aggregated market state at a point in time.
// This is the output of the Market Feed Service (Phase C1) and the input to
// strategy decision modules (Phase D).
type MarketSnapshot struct {
	Timestamp          time.Time
	Symbol             string
	Regime             RegimeType
	Signals            []SignalValue
	WallPositions      []WallPosition
	SignalHealth       SignalHealthSummary // per-signal health state (§6.3)
	MDC                MDCResult
	OrderBook          OrderBookSummary
	RegimeParams       RegimeParams
	DegradedReason     string             // human-readable reason for degradation
	FRR                float64            // Flash Return Rate
	HiddenRatio        float64            // estimated hidden order ratio
	CompetitorActivity float64            // 0-1 composite competitor activity score
	FRRTrend           float64            // G12: FRR trend signal [-1, +1]
	WeekendRatio       float64            // S6: historical weekend/weekday rate ratio
	FlashFreeze        bool               // true if flash crash detected, trading paused
	DegradedMode       bool               // true if operating in degraded mode (e.g., FRR-only)
	CascadePhase       string             // S5: liquidation cascade phase (early/mid/late/none)
}
