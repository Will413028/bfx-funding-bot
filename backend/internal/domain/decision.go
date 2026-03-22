package domain

// DecisionContext aggregates all inputs needed for strategy decision-making.
// Assembled by the worker before calling strategy modules.
type DecisionContext struct {
	Snapshot      *MarketSnapshot
	Config        *StrategyConfig
	Currency      string
	ActiveOffers  []FundingOffer
	ActiveCredits []FundingCredit
	Available     float64
	IdleMinutes   float64
}

// DecisionResult represents the output of strategy decision-making.
type DecisionResult struct {
	Currency     string          // e.g. "fUSD" — set by worker before execution
	Reason       string          // human-readable decision reason
	Offers       []OfferDecision // recommended offers to place
	StrategyTags []string        // strategy modules that contributed to this decision (§9.2)
	Cancels      []int64         // offer IDs to cancel
	RenewCredits []int64         // credit IDs to renew
	UseHidden    bool            // true → use hidden order flag (flags: 64) (§4.3)
}

// OfferDecision represents a single recommended offer.
type OfferDecision struct {
	Amount float64
	Rate   float64
	Period int
}
