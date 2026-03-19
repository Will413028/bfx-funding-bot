package domain

// DecisionContext aggregates all inputs needed for strategy decision-making.
// Assembled by the worker before calling strategy modules.
type DecisionContext struct {
	Snapshot      *MarketSnapshot
	Config        *StrategyConfig
	ActiveOffers  []FundingOffer
	ActiveCredits []FundingCredit
	Available     float64 // available balance in funding wallet
	Currency      string  // e.g. "fUSD"
}

// DecisionResult represents the output of strategy decision-making.
type DecisionResult struct {
	Currency     string          // e.g. "fUSD" — set by worker before execution
	Offers       []OfferDecision // recommended offers to place
	Cancels      []int64         // offer IDs to cancel
	RenewCredits []int64         // credit IDs to renew
	UseHidden    bool            // true → use hidden order flag (flags: 64) (§4.3)
	StrategyTags []string        // strategy modules that contributed to this decision (§9.2)
	Reason       string          // human-readable decision reason
}

// OfferDecision represents a single recommended offer.
type OfferDecision struct {
	Amount float64
	Rate   float64
	Period int
}
