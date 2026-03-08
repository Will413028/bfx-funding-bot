package strategy

import (
	"math"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	// Concentration threshold: single day > 50% of total → concentrated
	concentrationThreshold = 0.50
)

// StaggerStrategy analyzes credit expiry distribution and adjusts
// new offer period to spread out maturity dates.
type StaggerStrategy struct {
	now func() time.Time
}

// NewStaggerStrategy creates a new StaggerStrategy using real time.
func NewStaggerStrategy() *StaggerStrategy {
	return &StaggerStrategy{now: time.Now}
}

// Apply analyzes expiry distribution and returns adjusted period.
func (s *StaggerStrategy) Apply(ctx *domain.DecisionContext) *domain.DecisionResult {
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

	// No credits → use midpoint period
	if len(ctx.ActiveCredits) == 0 {
		midPeriod := (cfg.Period.Min + cfg.Period.Max) / 2
		return &domain.DecisionResult{
			Offers: []domain.OfferDecision{
				{Amount: amount, Rate: rate, Period: midPeriod},
			},
			Reason: "stagger:no_credits",
		}
	}

	// Build expiry buckets
	now := s.now()
	buckets := make(map[int]float64)
	totalAmount := 0.0

	for _, c := range ctx.ActiveCredits {
		expiryTime := c.OpenedAt.Add(time.Duration(c.Period) * 24 * time.Hour)
		daysUntil := int(math.Ceil(expiryTime.Sub(now).Hours() / 24))
		if daysUntil < 1 {
			daysUntil = 1
		}
		buckets[daysUntil] += c.Amount
		totalAmount += c.Amount
	}

	// Find best period: day with least expiry amount in [Min, Max]
	bestPeriod := cfg.Period.Min
	minAmount := math.MaxFloat64
	var emptyDays []int

	for day := cfg.Period.Min; day <= cfg.Period.Max; day++ {
		amt := buckets[day]
		if amt < minAmount {
			minAmount = amt
			bestPeriod = day
			emptyDays = []int{day}
		} else if amt == minAmount {
			emptyDays = append(emptyDays, day)
		}
	}

	// If multiple days tied, pick the middle one
	if len(emptyDays) > 1 {
		bestPeriod = emptyDays[len(emptyDays)/2]
	}

	// Concentration detection
	maxBucket := 0.0
	for _, amt := range buckets {
		if amt > maxBucket {
			maxBucket = amt
		}
	}

	reason := "stagger:balanced"
	if totalAmount > 0 && maxBucket/totalAmount > concentrationThreshold {
		reason = "stagger:concentrated"
	}

	return &domain.DecisionResult{
		Offers: []domain.OfferDecision{
			{Amount: amount, Rate: rate, Period: bestPeriod},
		},
		Reason: reason,
	}
}
