package strategy

import (
	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	// Balance thresholds for tier count
	twoTierThreshold   = 150.0
	threeTierThreshold = 1000.0

	// Allocation ratios per tier count
	twoTierCoreRatio = 0.70
	twoTierAggrRatio = 0.30

	threeCoreTierRatio = 0.50
	threeModTierRatio  = 0.30
	threeAggrTierRatio = 0.20

	// Rate multipliers per tier
	coreRateMul       = 1.0
	moderateRateMul   = 1.10
	aggressiveRateMul = 1.25

	// Minimum per-tier amount
	minTierAmount = 50.0
)

// tier defines a single allocation tier.
type tier struct {
	ratio   float64
	rateMul float64
}

var (
	singleTier = []tier{{ratio: 1.0, rateMul: coreRateMul}}
	twoTiers   = []tier{
		{ratio: twoTierCoreRatio, rateMul: coreRateMul},
		{ratio: twoTierAggrRatio, rateMul: aggressiveRateMul},
	}
	threeTiers = []tier{
		{ratio: threeCoreTierRatio, rateMul: coreRateMul},
		{ratio: threeModTierRatio, rateMul: moderateRateMul},
		{ratio: threeAggrTierRatio, rateMul: aggressiveRateMul},
	}
)

// AllocationStrategy splits available funds into tiers with different
// rate targets to diversify fill probability and yield.
type AllocationStrategy struct{}

// NewAllocationStrategy creates a new AllocationStrategy.
func NewAllocationStrategy() *AllocationStrategy {
	return &AllocationStrategy{}
}

// Apply determines the tier allocation and returns multiple OfferDecisions.
func (a *AllocationStrategy) Apply(ctx *domain.DecisionContext) *domain.DecisionResult {
	// Guard: no market data
	if ctx.Snapshot == nil {
		return &domain.DecisionResult{Reason: "no_snapshot"}
	}

	// Guard: flash freeze
	if ctx.Snapshot.FlashFreeze {
		return &domain.DecisionResult{Reason: "flash_freeze"}
	}

	// Guard: insufficient balance
	if ctx.Available < minBalance {
		return &domain.DecisionResult{Reason: "insufficient_balance"}
	}

	cfg := ctx.Config
	snap := ctx.Snapshot

	// Base rate: FRR or config.Rate.Min
	baseRate := snap.FRR
	if baseRate <= 0 {
		baseRate = cfg.Rate.Min
	}

	// Determine tiers
	tiers := a.selectTiers(ctx.Available, snap.Regime)

	// Build offers
	offers := make([]domain.OfferDecision, 0, len(tiers))
	for _, t := range tiers {
		amount := ctx.Available * t.ratio
		if amount > cfg.Amount.Max {
			amount = cfg.Amount.Max
		}
		rate := clamp(baseRate*t.rateMul, cfg.Rate.Min, cfg.Rate.Max)
		offers = append(offers, domain.OfferDecision{
			Amount: amount,
			Rate:   rate,
			Period: cfg.Period.Min,
		})
	}

	return &domain.DecisionResult{
		Offers: offers,
		Reason: "allocation",
	}
}

// selectTiers determines the tier configuration based on balance and regime.
func (a *AllocationStrategy) selectTiers(available float64, regime domain.RegimeType) []tier {
	// Regime caps
	maxTiers := 3
	switch regime {
	case domain.RegimeCrisis:
		maxTiers = 1
	case domain.RegimeBackwardation:
		maxTiers = 2
	}

	// Balance-based tier count
	tiers := a.tiersForBalance(available)

	// Cap by regime
	if len(tiers) > maxTiers {
		tiers = a.tiersForCount(maxTiers)
	}

	// Validate minimum amounts
	for _, t := range tiers {
		if available*t.ratio < minTierAmount {
			// Fall back to fewer tiers
			if len(tiers) > 2 {
				tiers = a.tiersForCount(2)
				// Re-check
				for _, t2 := range tiers {
					if available*t2.ratio < minTierAmount {
						return singleTier
					}
				}
				return tiers
			}
			return singleTier
		}
	}

	return tiers
}

// tiersForBalance returns tier config based on available balance.
func (a *AllocationStrategy) tiersForBalance(available float64) []tier {
	if available > threeTierThreshold {
		return threeTiers
	}
	if available >= twoTierThreshold {
		return twoTiers
	}
	return singleTier
}

// tiersForCount returns the appropriate tier config for a given count.
func (a *AllocationStrategy) tiersForCount(count int) []tier {
	switch count {
	case 1:
		return singleTier
	case 2:
		return twoTiers
	default:
		return threeTiers
	}
}
