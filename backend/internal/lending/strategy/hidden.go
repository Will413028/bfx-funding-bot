package strategy

import "github.com/will/bfx-funding-bot/backend/internal/domain"

const (
	// Minimum order size to evaluate hidden mode (§4.3)
	hiddenMinAmount = 5000.0

	// Hidden ratio threshold: above this → use hidden orders
	hiddenRatioThreshold = 0.30

	// Competitor activity threshold: above this → consider hidden
	competitorActivityThreshold = 0.60
)

// HiddenOfferStrategy decides whether to use hidden orders (flags: 64)
// based on competitor density and estimated hidden ratio.
type HiddenOfferStrategy struct{}

func NewHiddenOfferStrategy() *HiddenOfferStrategy {
	return &HiddenOfferStrategy{}
}

// ShouldHide returns true if the offer should use hidden flag.
func (h *HiddenOfferStrategy) ShouldHide(snap *domain.MarketSnapshot, amount float64) bool {
	if snap == nil || amount < hiddenMinAmount {
		return false
	}

	// High hidden ratio → many invisible bots, use hidden
	if snap.HiddenRatio > hiddenRatioThreshold {
		return true
	}

	// High competitor activity + decent hidden ratio → use hidden
	if snap.CompetitorActivity > competitorActivityThreshold && snap.HiddenRatio > 0.15 {
		return true
	}

	return false
}

// Apply evaluates the market and adds Hidden flag recommendation to the decision.
func (h *HiddenOfferStrategy) Apply(ctx *domain.DecisionContext) *domain.DecisionResult {
	if ctx.Snapshot == nil {
		return &domain.DecisionResult{Reason: "no_snapshot"}
	}
	if ctx.Snapshot.FlashFreeze {
		return &domain.DecisionResult{Reason: "flash_freeze"}
	}
	if ctx.Available < minBalance {
		return &domain.DecisionResult{Reason: "insufficient_balance"}
	}

	useHidden := h.ShouldHide(ctx.Snapshot, ctx.Available)

	return &domain.DecisionResult{
		UseHidden: useHidden,
		Reason:    "hidden_offer_eval",
	}
}
