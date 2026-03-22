package strategy

import (
	"math"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	// Maximum single order as fraction of ask depth
	depthFraction = 0.05

	// Maximum number of splits
	maxSplitCount = 5

	// Minimum amount per split order
	minSplitAmount = 50.0
)

// SplittingStrategy splits large orders into smaller ones based on
// order book depth to reduce market impact.
type SplittingStrategy struct{}

// NewSplittingStrategy creates a new SplittingStrategy.
func NewSplittingStrategy() *SplittingStrategy {
	return &SplittingStrategy{}
}

// Apply splits the available balance into depth-aware chunks.
func (s *SplittingStrategy) Apply(ctx *domain.DecisionContext) *domain.DecisionResult {
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
	amount := math.Min(ctx.Available, cfg.Amount.Max)

	// Base rate
	rate := snap.FRR
	if rate <= 0 {
		rate = cfg.Rate.Min
	}

	// Compute max per-order based on ask depth
	maxPerOrder := s.maxOrderSize(snap.OrderBook.AskDepth)

	// If no depth data or amount fits in one order, no split
	if maxPerOrder <= 0 || amount <= maxPerOrder {
		return &domain.DecisionResult{
			Offers: []domain.OfferDecision{
				{Amount: amount, Rate: rate, Period: cfg.Period.Min},
			},
			Reason: "splitting:no_split",
		}
	}

	// Calculate split count
	splitCount := int(math.Ceil(amount / maxPerOrder))

	// Cap at max splits
	if splitCount > maxSplitCount {
		splitCount = maxSplitCount
	}

	// Ensure each split >= minimum amount
	for splitCount > 1 && amount/float64(splitCount) < minSplitAmount {
		splitCount--
	}

	// Build split offers
	perSplit := amount / float64(splitCount)
	offers := make([]domain.OfferDecision, splitCount)
	for i := range offers {
		offers[i] = domain.OfferDecision{
			Amount: perSplit,
			Rate:   rate,
			Period: cfg.Period.Min,
		}
	}

	return &domain.DecisionResult{
		Offers: offers,
		Reason: "splitting",
	}
}

// maxOrderSize returns the maximum per-order amount based on ask depth.
func (s *SplittingStrategy) maxOrderSize(askDepth float64) float64 {
	if askDepth <= 0 {
		return 0
	}
	return askDepth * depthFraction
}

// ComputeSplits returns the number of splits for the given amount and ask depth.
// Returns 1 if no splitting needed. Used by CompositeStrategy.
func ComputeSplits(amount float64, askDepth float64) int {
	s := &SplittingStrategy{}
	maxPerOrder := s.maxOrderSize(askDepth)

	if maxPerOrder <= 0 || amount <= maxPerOrder {
		return 1
	}

	splitCount := int(math.Ceil(amount / maxPerOrder))
	if splitCount > maxSplitCount {
		splitCount = maxSplitCount
	}

	for splitCount > 1 && amount/float64(splitCount) < minSplitAmount {
		splitCount--
	}

	return splitCount
}
