package strategy

import (
	"math"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	// FRR relative floor: don't go below FRR × this ratio
	frrFloorRatio = 0.8

	// Regime floor multipliers on config.Rate.Min
	regimeCrisisFloorMul        = 1.5
	regimeBackwardationFloorMul = 1.2
)

// FloorStrategy computes a dynamic rate floor based on opportunity cost,
// FRR reference, and market regime. The floor represents the minimum
// acceptable rate — offers should not be placed below this.
type FloorStrategy struct{}

// NewFloorStrategy creates a new FloorStrategy.
func NewFloorStrategy() *FloorStrategy {
	return &FloorStrategy{}
}

// Apply computes the floor rate from three sources and returns the highest.
func (f *FloorStrategy) Apply(ctx *domain.DecisionContext) *domain.DecisionResult {
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

	// Layer 1: Opportunity cost floor (user's configured minimum)
	opportunityCost := cfg.Rate.Min

	// Layer 2: FRR relative floor
	frrFloor := snap.FRR * frrFloorRatio

	// Layer 3: Regime-adjusted floor
	regimeFloor := f.regimeFloor(cfg.Rate.Min, snap.Regime)

	// Take the maximum across all layers
	floor := opportunityCost
	reason := "floor:opportunity_cost"

	if frrFloor > floor {
		floor = frrFloor
		reason = "floor:frr_relative"
	}
	if regimeFloor > floor {
		floor = regimeFloor
		reason = "floor:regime"
	}

	return &domain.DecisionResult{
		Offers: []domain.OfferDecision{
			{
				Amount: math.Min(ctx.Available, cfg.Amount.Max),
				Rate:   floor,
				Period: cfg.Period.Min,
			},
		},
		Reason: reason,
	}
}

// regimeFloor computes the regime-adjusted floor based on config.Rate.Min.
func (f *FloorStrategy) regimeFloor(minRate float64, regime domain.RegimeType) float64 {
	switch regime {
	case domain.RegimeCrisis:
		return minRate * regimeCrisisFloorMul
	case domain.RegimeBackwardation:
		return minRate * regimeBackwardationFloorMul
	default: // contango, neutral
		return minRate
	}
}
