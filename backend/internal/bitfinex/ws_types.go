package bitfinex

import "time"

// FundingTicker represents a funding ticker snapshot (16 fields).
type FundingTicker struct {
	Symbol           string
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

// BookEntry represents a single funding order book entry.
type BookEntry struct {
	Symbol string
	Rate   float64
	Period int
	Count  int
	Amount float64 // >0 offer, <0 bid
}

// FundingTrade represents a funding trade event.
type FundingTrade struct {
	Symbol string
	ID     int64
	MTS    time.Time
	Amount float64 // >0 lend, <0 borrow
	Rate   float64
	Period int
}

// WSFundingOffer represents a funding offer from authenticated channel.
type WSFundingOffer struct {
	ID         int64
	Symbol     string
	Created    time.Time
	Updated    time.Time
	Amount     float64
	AmountOrig float64
	Type       string // "LIMIT", "FRRDELTA"
	Status     string // "ACTIVE", "EXECUTED", "PARTIALLY FILLED", "CANCELED"
	Rate       float64
	Period     int
	Notify     bool
	Hidden     bool
	Renew      bool
}

// WSFundingCredit represents a funding credit from authenticated channel.
type WSFundingCredit struct {
	ID       int64
	Symbol   string
	Side     int
	Created  time.Time
	Updated  time.Time
	Amount   float64
	Status   string
	Rate     float64
	Period   int
	Opened   time.Time
	Renew    bool
	NoClose  bool
	PositionPair string
}

// WSWallet represents a wallet update from authenticated channel.
type WSWallet struct {
	Type      string // "exchange", "margin", "funding"
	Currency  string
	Balance   float64
	Unsettled float64
	Available float64
}

// WSNotification represents a notification from authenticated channel.
type WSNotification struct {
	MTS        time.Time
	Type       string
	MessageID  int64
	NotifyInfo []byte // raw JSON
	Code       int
	Status     string // "SUCCESS", "ERROR", "FAILURE"
	Text       string
}

// ChannelInfo holds the mapping between a chanId and its subscription.
type ChannelInfo struct {
	Channel string // "ticker", "book", "trades"
	Symbol  string // "fUSD"
	Prec    string // book precision "P0"-"P4"
	Len     int    // book depth
}

// Subscription stores info needed to resubscribe after reconnection.
type Subscription struct {
	Channel string
	Symbol  string
	Prec    string
	Len     int
}

// EventHandlers holds callback functions for WebSocket events.
type EventHandlers struct {
	// Connection events
	OnConnect    func()
	OnDisconnect func(error)
	OnError      func(error)

	// Public channel - Ticker
	OnTicker func(FundingTicker)

	// Public channel - Book
	OnBookSnapshot func(string, []BookEntry) // symbol, entries
	OnBookUpdate   func(string, BookEntry)   // symbol, entry

	// Public channel - Trades
	OnTradeSnapshot  func(string, []FundingTrade) // symbol, trades
	OnTradeExecuted  func(string, FundingTrade)   // symbol, trade
	OnTradeUpdated   func(string, FundingTrade)   // symbol, trade

	// Auth channel - Funding Offers
	OnFundingOfferSnapshot func([]WSFundingOffer)
	OnFundingOfferNew      func(WSFundingOffer)
	OnFundingOfferUpdate   func(WSFundingOffer)
	OnFundingOfferCancel   func(WSFundingOffer)

	// Auth channel - Funding Credits
	OnFundingCreditSnapshot func([]WSFundingCredit)
	OnFundingCreditNew      func(WSFundingCredit)
	OnFundingCreditUpdate   func(WSFundingCredit)
	OnFundingCreditClose    func(WSFundingCredit)

	// Auth channel - Wallets
	OnWalletSnapshot func([]WSWallet)
	OnWalletUpdate   func(WSWallet)

	// Auth channel - Notifications
	OnNotification func(WSNotification)
}
