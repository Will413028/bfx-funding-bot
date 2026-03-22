package strategy

import (
	"math"
	"math/rand/v2"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
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
	// Guard: nil snapshot — no market data, skip this tick
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

	// Base rate
	rate := snap.FRR
	if rate <= 0 {
		rate = cfg.Rate.Min
	}

	amount := math.Min(ctx.Available, cfg.Amount.Max)

	// S3: psychological price avoidance only
	rate = avoidPsychLevel(rate)

	// Clamp rate
	rate = clamp(rate, cfg.Rate.Min, cfg.Rate.Max)

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

// ApplyNoise applies psychological price avoidance to rate.
// Returns adjusted (rate, amount).
// Used by CompositeStrategy.
func ApplyNoise(rate float64, amount float64, rateMin float64, rateMax float64) (float64, float64) {
	// S3: Only psychological price avoidance, no random perturbation
	rate = avoidPsychLevel(rate)
	rate = clamp(rate, rateMin, rateMax)
	return rate, amount
}
