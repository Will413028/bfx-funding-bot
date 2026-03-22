package strategy

import (
	"math"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	// Queue ratio thresholds
	queueLowThreshold  = 0.20 // 20%
	queueHighThreshold = 0.50 // 50%

	// Rate discounts
	queueModerateDiscount = 0.98 // -2%
	queueDeepDiscount     = 0.95 // -5%

	// Stale offer: rate > bestAsk + staleSpreadMul × spread
	staleSpreadMul = 2.0

	// Sigmoid queue discount parameters (GT6)
	maxQueueDiscount = 0.05 // max 5% discount at full queue
	sigmoidEdgeLow   = 0.10 // queue ratio where discount starts
	sigmoidEdgeHigh  = 0.60 // queue ratio where discount maxes out
)

// QueueStrategy estimates the queue position of the user's offers
// and adjusts rate or recommends cancellations to improve fill probability.
type QueueStrategy struct{}

// NewQueueStrategy creates a new QueueStrategy.
func NewQueueStrategy() *QueueStrategy {
	return &QueueStrategy{}
}

// Apply evaluates queue position and returns adjusted rate and cancel suggestions.
func (q *QueueStrategy) Apply(ctx *domain.DecisionContext) *domain.DecisionResult {
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
	ob := snap.OrderBook

	// Base rate
	rate := snap.FRR
	if rate <= 0 {
		rate = cfg.Rate.Min
	}

	amount := math.Min(ctx.Available, cfg.Amount.Max)

	// No data → skip queue adjustment
	if ob.AskDepth <= 0 || ob.Spread <= 0 {
		return &domain.DecisionResult{
			Offers: []domain.OfferDecision{
				{Amount: amount, Rate: rate, Period: cfg.Period.Min},
			},
			Reason: "queue:no_data",
		}
	}

	// Queue depth estimation
	queueDepth := ob.AskDepth * (rate - ob.BestAsk) / ob.Spread
	if queueDepth < 0 {
		queueDepth = 0
	}

	queueRatio := queueDepth / ob.AskDepth

	// Apply discount based on queue ratio
	adjustedRate := rate
	reason := "queue:low"

	if queueRatio > queueHighThreshold {
		adjustedRate = rate * queueDeepDiscount
		reason = "queue:deep"
	} else if queueRatio > queueLowThreshold {
		adjustedRate = rate * queueModerateDiscount
		reason = "queue:moderate"
	}

	adjustedRate = clamp(adjustedRate, cfg.Rate.Min, cfg.Rate.Max)

	// Stale offer detection
	staleThreshold := ob.BestAsk + staleSpreadMul*ob.Spread
	var cancels []int64
	for _, o := range ctx.ActiveOffers {
		if o.Rate > staleThreshold {
			cancels = append(cancels, o.ID)
		}
	}

	return &domain.DecisionResult{
		Offers: []domain.OfferDecision{
			{Amount: amount, Rate: adjustedRate, Period: cfg.Period.Min},
		},
		Cancels: cancels,
		Reason:  reason,
	}
}

// ComputeQueueDiscount returns the rate discount multiplier based on queue depth.
// Uses smoothstep sigmoid for continuous adjustment (GT6).
// Returns 1.0 if no adjustment needed. Used by CompositeStrategy.
func ComputeQueueDiscount(rate float64, ob domain.OrderBookSummary) float64 {
	if ob.AskDepth <= 0 || ob.Spread <= 0 {
		return 1.0
	}

	queueDepth := ob.AskDepth * (rate - ob.BestAsk) / ob.Spread
	if queueDepth < 0 {
		queueDepth = 0
	}

	queueRatio := queueDepth / ob.AskDepth

	// Smoothstep sigmoid: continuous transition from 1.0 to (1.0 - maxQueueDiscount)
	t := smoothstep(queueRatio, sigmoidEdgeLow, sigmoidEdgeHigh)
	return 1.0 - maxQueueDiscount*t
}

// smoothstep performs Hermite interpolation between 0 and 1.
func smoothstep(x, edge0, edge1 float64) float64 {
	t := (x - edge0) / (edge1 - edge0)
	if t < 0 {
		t = 0
	}
	if t > 1 {
		t = 1
	}
	return t * t * (3 - 2*t)
}

// DetectStaleOffers returns IDs of offers whose rate exceeds the stale threshold.
// Used by CompositeStrategy.
func DetectStaleOffers(offers []domain.FundingOffer, ob domain.OrderBookSummary) []int64 {
	if ob.BestAsk <= 0 || ob.Spread <= 0 {
		return nil
	}

	staleThreshold := ob.BestAsk + staleSpreadMul*ob.Spread
	var cancels []int64
	for _, o := range offers {
		if o.Rate > staleThreshold {
			cancels = append(cancels, o.ID)
		}
	}
	return cancels
}
