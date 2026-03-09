package strategy

import (
	"math"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	// Fill proxy thresholds
	fillProxyLow  = 0.30
	fillProxyHigh = 0.70

	// Amount multipliers
	partialLowMul  = 0.80 // reduce 20%
	partialHighMul = 1.20 // increase 20%
)

// PartialStrategy detects partial fill residuals and adjusts
// new offer amounts based on market fill activity.
type PartialStrategy struct{}

// NewPartialStrategy creates a new PartialStrategy.
func NewPartialStrategy() *PartialStrategy {
	return &PartialStrategy{}
}

// Apply detects residuals, computes fill proxy, and adjusts amount.
func (p *PartialStrategy) Apply(ctx *domain.DecisionContext) *domain.DecisionResult {
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

	// Detect residuals and compute fill proxy
	var cancels []int64
	var validSum float64
	var validCount int

	for _, o := range ctx.ActiveOffers {
		if o.Amount < minBalance {
			cancels = append(cancels, o.ID)
		} else {
			validSum += o.Amount
			validCount++
		}
	}

	// Fill proxy: 1 - (avgOfferSize / Amount.Max)
	fillProxy := 0.0
	if validCount > 0 && cfg.Amount.Max > 0 {
		avgSize := validSum / float64(validCount)
		fillProxy = 1.0 - (avgSize / cfg.Amount.Max)
		if fillProxy < 0 {
			fillProxy = 0
		}
	}

	// Adjust amount based on fill proxy
	if fillProxy > fillProxyHigh {
		amount *= partialHighMul
	} else if fillProxy < fillProxyLow {
		amount *= partialLowMul
	}

	// Cap to config max and available
	amount = math.Min(amount, cfg.Amount.Max)
	amount = math.Min(amount, ctx.Available)

	// Ensure minimum
	if amount < minBalance {
		return &domain.DecisionResult{
			Cancels: cancels,
			Reason:  "partial:too_small",
		}
	}

	reason := "partial:normal"
	if fillProxy > fillProxyHigh {
		reason = "partial:high_activity"
	} else if fillProxy < fillProxyLow {
		reason = "partial:low_activity"
	}

	return &domain.DecisionResult{
		Offers: []domain.OfferDecision{
			{Amount: amount, Rate: rate, Period: cfg.Period.Min},
		},
		Cancels: cancels,
		Reason:  reason,
	}
}
