package strategy

import (
	"math"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	// Impact ratio thresholds
	impactLowThreshold  = 0.10 // 10%
	impactHighThreshold = 0.25 // 25%

	// Rate premiums
	impactModeratePremium = 1.03 // +3%
	impactHighPremium     = 1.05 // +5%
)

// ImpactStrategy evaluates the market impact of the user's offers
// and adjusts rate/amount to minimize price disruption.
type ImpactStrategy struct{}

// NewImpactStrategy creates a new ImpactStrategy.
func NewImpactStrategy() *ImpactStrategy {
	return &ImpactStrategy{}
}

// Apply evaluates impact and returns adjusted rate and amount.
func (m *ImpactStrategy) Apply(ctx *domain.DecisionContext) *domain.DecisionResult {
	// Guard: flash freeze
	if ctx.Snapshot != nil && ctx.Snapshot.FlashFreeze {
		return &domain.DecisionResult{Reason: "flash_freeze"}
	}

	// Guard: insufficient balance
	if ctx.Available < minBalance {
		return &domain.DecisionResult{Reason: "insufficient_balance"}
	}

	cfg := ctx.Config
	snap := ctx.Snapshot

	// Base rate
	rate := snap.FRR
	if rate <= 0 {
		rate = cfg.Rate.Min
	}

	amount := math.Min(ctx.Available, cfg.Amount.Max)
	askDepth := snap.OrderBook.AskDepth

	// No depth data → skip impact adjustment
	if askDepth <= 0 {
		return &domain.DecisionResult{
			Offers: []domain.OfferDecision{
				{Amount: amount, Rate: rate, Period: cfg.Period.Min},
			},
			Reason: "impact:no_data",
		}
	}

	// Sum existing offer amounts
	existingAmount := 0.0
	for _, o := range ctx.ActiveOffers {
		existingAmount += o.Amount
	}

	// Impact ratio
	impactRatio := (existingAmount + amount) / askDepth

	if impactRatio <= impactLowThreshold {
		// Low impact — no adjustment
		return &domain.DecisionResult{
			Offers: []domain.OfferDecision{
				{Amount: amount, Rate: rate, Period: cfg.Period.Min},
			},
			Reason: "impact:low",
		}
	}

	if impactRatio <= impactHighThreshold {
		// Moderate impact — rate premium only
		adjustedRate := clamp(rate*impactModeratePremium, cfg.Rate.Min, cfg.Rate.Max)
		return &domain.DecisionResult{
			Offers: []domain.OfferDecision{
				{Amount: amount, Rate: adjustedRate, Period: cfg.Period.Min},
			},
			Reason: "impact:moderate",
		}
	}

	// High impact — rate premium + amount reduction
	maxAllowed := impactHighThreshold*askDepth - existingAmount
	if maxAllowed < minBalance {
		// Already over limit
		return &domain.DecisionResult{Reason: "impact:over_limit"}
	}

	reducedAmount := math.Min(amount, maxAllowed)
	adjustedRate := clamp(rate*impactHighPremium, cfg.Rate.Min, cfg.Rate.Max)

	return &domain.DecisionResult{
		Offers: []domain.OfferDecision{
			{Amount: reducedAmount, Rate: adjustedRate, Period: cfg.Period.Min},
		},
		Reason: "impact:high",
	}
}
