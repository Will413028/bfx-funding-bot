package strategy

import (
	"math"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	// MDC premium mapping: linear interpolation
	// MDC +1 → maxPremium, MDC 0 → 1.0, MDC -1 → minPremium
	maxPremiumUp   = 0.5  // positive MDC premium range (1.0 → 1.5)
	maxPremiumDown = 0.18 // negative MDC discount range (1.0 → 0.82) — GT2: 0.30→0.18, backwardation 降價過慷慨

	// Regime multipliers
	regimeContangoMul       = 1.05 // +5%
	regimeBackwardationMul  = 0.90 // -10%

	// Order book pressure
	depthRatioThreshold = 1.5
	depthPressureBoost  = 1.02 // +2%

	// Wall avoidance
	wallProximityPct  = 0.10 // 10% of rate
	wallAvoidDiscount = 0.99 // -1%

	// Minimum balance
	minBalance = 50.0

	// Adaptive Deviation Guard (§8.1): max deviation from FRR per regime
	deviationContango       = 0.40 // bull: 40%
	deviationBackwardation  = 0.20 // bear: 20%
	deviationNeutral        = 0.25 // neutral: 25%
	deviationCrisis         = 0.60 // crisis: 60%
	deviationHardCeiling    = 0.80 // absolute max: 80%

	// Bitfinex funding fee (15% of earnings)
	// M1: All rate comparisons should use netRate = rate × (1 - FeeRate)
	FeeRate = 0.15
)

// PricingStrategy computes the recommended offer rate based on market state.
// Satisfies the worker.Strategy interface via Apply(*DecisionContext) *DecisionResult.
type PricingStrategy struct{}

// NewPricingStrategy creates a new PricingStrategy.
func NewPricingStrategy() *PricingStrategy {
	return &PricingStrategy{}
}

// Apply evaluates the current market state and returns a decision with the recommended rate.
func (p *PricingStrategy) Apply(ctx *domain.DecisionContext) *domain.DecisionResult {
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

	snap := ctx.Snapshot
	cfg := ctx.Config

	// Step 1: Base rate (FRR preferred, fallback to mid-rate)
	baseRate := p.selectBaseRate(snap)

	// Step 2: MDC premium
	rate := baseRate * p.mdcMultiplier(snap.MDC.Score)

	// Step 3: Regime adjustment
	rate = p.applyRegime(rate, snap.Regime, cfg)

	// Step 4: Order book pressure
	rate = p.applyDepthPressure(rate, snap.OrderBook)

	// Step 5: Wall avoidance
	rate = p.applyWallAvoidance(rate, snap.WallPositions)

	// Step 6: Adaptive Deviation Guard (§8.1)
	rate = p.applyDeviationGuard(rate, baseRate, snap.Regime)

	// Step 7: Clamp to config bounds
	rate = clamp(rate, cfg.Rate.Min, cfg.Rate.Max)

	return &domain.DecisionResult{
		Offers: []domain.OfferDecision{
			{
				Amount: math.Min(ctx.Available, cfg.Amount.Max),
				Rate:   rate,
				Period: cfg.Period.Min,
			},
		},
		StrategyTags: []string{"pricing"},
		Reason:       "pricing",
	}
}

// selectBaseRate picks FRR if available, otherwise order book mid-rate.
func (p *PricingStrategy) selectBaseRate(snap *domain.MarketSnapshot) float64 {
	if snap.FRR > 0 {
		return snap.FRR
	}
	return snap.OrderBook.MidRate
}

// mdcMultiplier maps MDC score [-1, +1] to a premium multiplier.
// MDC +1 → 1.0 + maxPremiumUp (1.5)
// MDC  0 → 1.0
// MDC -1 → 1.0 - maxPremiumDown (0.7)
func (p *PricingStrategy) mdcMultiplier(score float64) float64 {
	if score >= 0 {
		return 1.0 + score*maxPremiumUp
	}
	return 1.0 + score*maxPremiumDown
}

// applyRegime adjusts rate based on market regime.
func (p *PricingStrategy) applyRegime(rate float64, regime domain.RegimeType, cfg *domain.StrategyConfig) float64 {
	switch regime {
	case domain.RegimeContango:
		return rate * regimeContangoMul
	case domain.RegimeBackwardation:
		return rate * regimeBackwardationMul
	case domain.RegimeCrisis:
		return cfg.Rate.Min
	default: // neutral
		return rate
	}
}

// applyDepthPressure adjusts rate based on bid/ask depth ratio.
func (p *PricingStrategy) applyDepthPressure(rate float64, ob domain.OrderBookSummary) float64 {
	if ob.AskDepth == 0 {
		return rate
	}
	ratio := ob.BidDepth / ob.AskDepth
	if ratio > depthRatioThreshold {
		return rate * depthPressureBoost
	}
	return rate
}

// applyWallAvoidance reduces rate slightly if an offer wall is near the calculated rate.
func (p *PricingStrategy) applyWallAvoidance(rate float64, walls []domain.WallPosition) float64 {
	for _, w := range walls {
		if w.Side != "offer" {
			continue
		}
		distance := math.Abs(w.Rate-rate) / rate
		if distance < wallProximityPct {
			return rate * wallAvoidDiscount
		}
	}
	return rate
}

// applyDeviationGuard clamps the rate so it doesn't deviate from the base rate
// (FRR) by more than a regime-dependent ceiling. Hard ceiling of 80% always applies.
func (p *PricingStrategy) applyDeviationGuard(rate, baseRate float64, regime domain.RegimeType) float64 {
	if baseRate <= 0 {
		return rate
	}

	ceiling := deviationNeutral
	switch regime {
	case domain.RegimeContango:
		ceiling = deviationContango
	case domain.RegimeBackwardation:
		ceiling = deviationBackwardation
	case domain.RegimeCrisis:
		ceiling = deviationCrisis
	}

	// Hard ceiling always applies
	ceiling = math.Min(ceiling, deviationHardCeiling)

	maxRate := baseRate * (1.0 + ceiling)
	minRate := baseRate * (1.0 - ceiling)

	return clamp(rate, minRate, maxRate)
}

func clamp(v, lo, hi float64) float64 {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

// ComputeBaseRate computes the MDC-adjusted rate with regime, depth pressure,
// and wall avoidance applied. This is the core pricing logic used by CompositeStrategy.
func ComputeBaseRate(snap *domain.MarketSnapshot, cfg *domain.StrategyConfig) float64 {
	p := &PricingStrategy{}

	baseRate := p.selectBaseRate(snap)
	rate := baseRate * p.mdcMultiplier(snap.MDC.Score)
	rate = p.applyRegime(rate, snap.Regime, cfg)
	rate = p.applyDepthPressure(rate, snap.OrderBook)
	rate = p.applyWallAvoidance(rate, snap.WallPositions)

	return rate
}
