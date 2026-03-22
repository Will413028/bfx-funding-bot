package strategy

import (
	"math"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	// Base opportunity cost per day (0.5 bps)
	baseCostPerDay = 0.000005

	// Volatility amplification
	lockupVolMultiplier = 5.0

	// Regime amplification on lockup cost
	lockupCrisisMul        = 2.0
	lockupBackwardationMul = 1.5

	// Maximum lockup cost as fraction of rate before suggesting shorter period
	maxCostRatio = 0.20
)

// LockupStrategy computes the opportunity cost of locking funds for a given
// period. Outputs a rate premium and optionally suggests a shorter period
// when the lockup cost is disproportionate to the rate.
type LockupStrategy struct{}

// NewLockupStrategy creates a new LockupStrategy.
func NewLockupStrategy() *LockupStrategy {
	return &LockupStrategy{}
}

// Apply computes the lockup cost and returns adjusted rate and period.
func (l *LockupStrategy) Apply(ctx *domain.DecisionContext) *domain.DecisionResult {
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

	// Use FRR as base rate reference; fallback to config min
	baseRate := snap.FRR
	if baseRate <= 0 {
		baseRate = cfg.Rate.Min
	}

	period := cfg.Period.Min

	// Compute lockup cost
	cost := l.computeCost(period, snap.RegimeParams.Volatility, snap.Regime)

	// Check if cost exceeds threshold → suggest shorter period
	if baseRate > 0 && cost/baseRate > maxCostRatio {
		period = l.suggestPeriod(baseRate, snap.RegimeParams.Volatility, snap.Regime)
		period = clampInt(period, cfg.Period.Min, cfg.Period.Max)
		cost = l.computeCost(period, snap.RegimeParams.Volatility, snap.Regime)
	}

	// Output: base rate + lockup premium
	adjustedRate := clamp(baseRate+cost, cfg.Rate.Min, cfg.Rate.Max)

	return &domain.DecisionResult{
		Offers: []domain.OfferDecision{
			{
				Amount: math.Min(ctx.Available, cfg.Amount.Max),
				Rate:   adjustedRate,
				Period: period,
			},
		},
		Reason: "lockup",
	}
}

// computeCost calculates the lockup opportunity cost.
func (l *LockupStrategy) computeCost(period int, volatility float64, regime domain.RegimeType) float64 {
	volAmplifier := 1.0 + lockupVolMultiplier*volatility
	cost := baseCostPerDay * float64(period) * volAmplifier
	cost *= l.regimeMultiplier(regime)
	return cost
}

// suggestPeriod finds the maximum period where lockup cost / rate <= maxCostRatio.
func (l *LockupStrategy) suggestPeriod(rate float64, volatility float64, regime domain.RegimeType) int {
	volAmplifier := 1.0 + lockupVolMultiplier*volatility
	regimeMul := l.regimeMultiplier(regime)
	// cost = baseCostPerDay × period × volAmplifier × regimeMul
	// cost / rate <= maxCostRatio
	// period <= maxCostRatio × rate / (baseCostPerDay × volAmplifier × regimeMul)
	denominator := baseCostPerDay * volAmplifier * regimeMul
	if denominator <= 0 {
		return 2
	}
	maxPeriod := maxCostRatio * rate / denominator
	return int(math.Floor(maxPeriod))
}

// regimeMultiplier returns the regime amplification factor for lockup cost.
func (l *LockupStrategy) regimeMultiplier(regime domain.RegimeType) float64 {
	switch regime {
	case domain.RegimeCrisis:
		return lockupCrisisMul
	case domain.RegimeBackwardation:
		return lockupBackwardationMul
	default:
		return 1.0
	}
}

// ComputeLockupPremium computes the lockup opportunity cost premium and
// optionally adjusts the period if cost exceeds the threshold.
// Used by CompositeStrategy.
func ComputeLockupPremium(baseRate float64, period int, volatility float64, regime domain.RegimeType) (premium float64, adjustedPeriod int) {
	l := &LockupStrategy{}

	cost := l.computeCost(period, volatility, regime)

	adjustedPeriod = period
	if baseRate > 0 && cost/baseRate > maxCostRatio {
		adjustedPeriod = l.suggestPeriod(baseRate, volatility, regime)
		cost = l.computeCost(adjustedPeriod, volatility, regime)
	}

	return cost, adjustedPeriod
}
