package strategy

import "github.com/will/bfx-funding-bot/backend/internal/domain"

const (
	// Threshold below which period preference is reduced (§5.6)
	holdRateThreshold = 0.60

	// Minimum dampening factor when hold rate is very low
	minHoldDampening = 0.50
)

// EarlyReturnAdjuster penalizes periods where borrowers historically return
// funds early. effective_return = rate × hold_rate. If hold_rate < 60%,
// the period preference weight is dampened.
type EarlyReturnAdjuster struct{}

func NewEarlyReturnAdjuster() *EarlyReturnAdjuster {
	return &EarlyReturnAdjuster{}
}

// AdjustPeriodWeight returns a dampening factor (0.5–1.0) for a given period
// based on the historical hold rate. If hold rate >= 60%, returns 1.0 (no change).
// holdRate is expected to be in [0.0, 1.0].
func (a *EarlyReturnAdjuster) AdjustPeriodWeight(holdRate float64) float64 {
	if holdRate >= holdRateThreshold {
		return 1.0
	}
	if holdRate <= 0 {
		return minHoldDampening
	}

	// Linear interpolation: holdRate 0 → 0.5, holdRate 0.6 → 1.0
	return minHoldDampening + (1.0-minHoldDampening)*(holdRate/holdRateThreshold)
}

// EffectiveReturn computes the expected return accounting for early return risk.
func (a *EarlyReturnAdjuster) EffectiveReturn(rate, holdRate float64) float64 {
	return rate * holdRate
}

// Apply evaluates whether the current period should be shortened based on hold rates.
// holdRates maps period (days) → historical hold rate [0.0, 1.0].
func (a *EarlyReturnAdjuster) Apply(ctx *domain.DecisionContext, holdRates map[int]float64) *domain.DecisionResult {
	if ctx.Snapshot == nil {
		return &domain.DecisionResult{Reason: "no_snapshot"}
	}
	if ctx.Snapshot.FlashFreeze {
		return &domain.DecisionResult{Reason: "flash_freeze"}
	}

	// Find the worst hold rate among configured period range
	worstHoldRate := 1.0
	for period := ctx.Config.Period.Min; period <= ctx.Config.Period.Max; period++ {
		if hr, ok := holdRates[period]; ok && hr < worstHoldRate {
			worstHoldRate = hr
		}
	}

	weight := a.AdjustPeriodWeight(worstHoldRate)

	return &domain.DecisionResult{
		Reason: "early_return_eval",
		Offers: []domain.OfferDecision{
			{
				Period: ctx.Config.Period.Min,
				Rate:   weight, // encode dampening as rate for downstream use
			},
		},
	}
}
