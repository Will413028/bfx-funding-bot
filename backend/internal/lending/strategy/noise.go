package strategy

import (
	"math"
	"math/rand/v2"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	// Noise ranges
	rateNoiseRange   = 0.01 // ±1%
	amountNoiseRange = 0.02 // ±2%

	// Psychological price avoidance
	psychStep      = 0.0005  // multiples to avoid
	psychTolerance = 0.00001 // proximity threshold
	psychShift     = 0.00002 // shift amount
)

// NoiseStrategy adds random perturbation to rate and amount,
// and avoids psychological price levels.
type NoiseStrategy struct {
	randFloat func() float64 // returns uniform [0, 1)
}

// NewNoiseStrategy creates a new NoiseStrategy with default randomness.
func NewNoiseStrategy() *NoiseStrategy {
	return &NoiseStrategy{randFloat: rand.Float64}
}

// Apply adds noise to rate and amount, avoids round numbers.
func (n *NoiseStrategy) Apply(ctx *domain.DecisionContext) *domain.DecisionResult {
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

	// Rate noise: ±1%
	rateNoise := rate * rateNoiseRange * (2*n.randFloat() - 1)
	rate += rateNoise

	// Psychological price avoidance
	rate = avoidPsychLevel(rate)

	// Clamp rate
	rate = clamp(rate, cfg.Rate.Min, cfg.Rate.Max)

	// Amount noise: ±2%
	amountNoise := amount * amountNoiseRange * (2*n.randFloat() - 1)
	amount += amountNoise

	// Clamp amount
	amount = math.Min(amount, cfg.Amount.Max)
	amount = math.Min(amount, ctx.Available)
	if amount < minBalance {
		return &domain.DecisionResult{Reason: "noise:too_small"}
	}

	return &domain.DecisionResult{
		Offers: []domain.OfferDecision{
			{Amount: amount, Rate: rate, Period: cfg.Period.Min},
		},
		Reason: "noise:applied",
	}
}

// avoidPsychLevel shifts rate away from multiples of psychStep.
func avoidPsychLevel(rate float64) float64 {
	nearest := math.Round(rate/psychStep) * psychStep
	if math.Abs(rate-nearest) < psychTolerance {
		return rate + psychShift
	}
	return rate
}
