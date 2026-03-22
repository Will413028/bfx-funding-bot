package strategy

import (
	"math"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	// Regime → base period ratio within [Period.Min, Period.Max]
	regimeContangoPeriodRatio       = 0.88 // GT3: 0.75→0.88, contango 是高利率窗口，更積極鎖長期
	regimeNeutralPeriodRatio        = 0.50
	regimeBackwardationPeriodRatio  = 0.25
	// crisis → 0.0 (use Period.Min)

	// Rate scaling: ±30% adjustment based on rate/FRR ratio
	rateScaleWeight = 0.3
	rateRatioCap    = 2.0

	// Volatility discount: kicks in above this threshold
	volThreshold   = 0.10
	volMaxDiscount = 0.20 // max volatility above threshold considered
	volMultiplier  = 2.0  // discount = (vol - threshold) × multiplier
)

// PeriodStrategy computes the recommended offer period (days) based on
// market regime, rate attractiveness, and volatility.
type PeriodStrategy struct{}

// NewPeriodStrategy creates a new PeriodStrategy.
func NewPeriodStrategy() *PeriodStrategy {
	return &PeriodStrategy{}
}

// Apply computes the optimal period and returns a DecisionResult.
func (p *PeriodStrategy) Apply(ctx *domain.DecisionContext) *domain.DecisionResult {
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

	// Step 1: Regime-driven base period
	periodRange := float64(cfg.Period.Max - cfg.Period.Min)
	ratio := p.regimeRatio(snap.Regime)
	basePeriod := float64(cfg.Period.Min) + ratio*periodRange

	// Step 2: Rate-based scaling
	basePeriod = p.applyRateScaling(basePeriod, ctx, snap)

	// Step 3: Volatility discount
	basePeriod = p.applyVolatilityDiscount(basePeriod, snap.RegimeParams.Volatility)

	// Step 4: Round and clamp
	period := int(math.Round(basePeriod))
	period = clampInt(period, cfg.Period.Min, cfg.Period.Max)

	return &domain.DecisionResult{
		Offers: []domain.OfferDecision{
			{
				Amount: math.Min(ctx.Available, cfg.Amount.Max),
				Rate:   cfg.Rate.Min, // placeholder; pricing sets the actual rate
				Period: period,
			},
		},
		Reason: "period",
	}
}

// regimeRatio returns the base period ratio for the given regime.
func (p *PeriodStrategy) regimeRatio(regime domain.RegimeType) float64 {
	switch regime {
	case domain.RegimeContango:
		return regimeContangoPeriodRatio
	case domain.RegimeNeutral:
		return regimeNeutralPeriodRatio
	case domain.RegimeBackwardation:
		return regimeBackwardationPeriodRatio
	case domain.RegimeCrisis:
		return 0.0
	default:
		return regimeNeutralPeriodRatio
	}
}

// applyRateScaling adjusts period based on offered rate vs FRR.
func (p *PeriodStrategy) applyRateScaling(period float64, ctx *domain.DecisionContext, snap *domain.MarketSnapshot) float64 {
	if snap.FRR <= 0 {
		return period
	}
	// Use the first offer's rate if available, otherwise config mid-rate
	offeredRate := ctx.Config.Rate.Min
	if len(ctx.ActiveOffers) > 0 {
		offeredRate = ctx.ActiveOffers[0].Rate
	}
	if offeredRate <= 0 {
		return period
	}

	rateRatio := math.Min(offeredRate/snap.FRR, rateRatioCap)
	scaleFactor := 1.0 + (rateRatio-1.0)*rateScaleWeight
	return period * scaleFactor
}

// applyVolatilityDiscount reduces period during high volatility.
func (p *PeriodStrategy) applyVolatilityDiscount(period float64, volatility float64) float64 {
	if volatility <= volThreshold {
		return period
	}
	excess := math.Min(volatility-volThreshold, volMaxDiscount)
	discount := 1.0 - excess*volMultiplier
	return period * discount
}

func clampInt(v, lo, hi int) int {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

// ComputePeriod computes the optimal lending period based on regime, rate
// attractiveness, and volatility. Used by CompositeStrategy.
func ComputePeriod(regime domain.RegimeType, volatility float64, offeredRate float64, frr float64, cfg *domain.StrategyConfig) int {
	p := &PeriodStrategy{}

	periodRange := float64(cfg.Period.Max - cfg.Period.Min)
	ratio := p.regimeRatio(regime)
	basePeriod := float64(cfg.Period.Min) + ratio*periodRange

	// Rate scaling
	if frr > 0 && offeredRate > 0 {
		rateRatio := math.Min(offeredRate/frr, rateRatioCap)
		scaleFactor := 1.0 + (rateRatio-1.0)*rateScaleWeight
		basePeriod *= scaleFactor
	}

	// Volatility discount
	basePeriod = p.applyVolatilityDiscount(basePeriod, volatility)

	period := int(math.Round(basePeriod))
	return clampInt(period, cfg.Period.Min, cfg.Period.Max)
}
