package strategy

import (
	"math"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	weekendFridayPremium   = 1.02 // +2%
	weekendSaturdayPremium = 1.05 // +5%
	weekendSundayPremium   = 1.03 // +3%

	fridayEveningHour = 18 // UTC
)

// WeekendStrategy applies a declining rate premium during weekends
// to compensate for reduced liquidity.
type WeekendStrategy struct {
	now func() time.Time
}

// NewWeekendStrategy creates a new WeekendStrategy using real time.
func NewWeekendStrategy() *WeekendStrategy {
	return &WeekendStrategy{now: time.Now}
}

// Apply evaluates the day of week and returns a premium-adjusted rate.
func (w *WeekendStrategy) Apply(ctx *domain.DecisionContext) *domain.DecisionResult {
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

	// Determine timestamp
	ts := snap.Timestamp
	if ts.IsZero() {
		ts = w.now()
	}
	ts = ts.UTC()

	// Weekend premium
	premium := 1.0
	reason := "weekend:weekday"

	switch ts.Weekday() {
	case time.Friday:
		if ts.Hour() >= fridayEveningHour {
			premium = weekendFridayPremium
			reason = "weekend:friday_eve"
		}
	case time.Saturday:
		premium = weekendSaturdayPremium
		reason = "weekend:saturday"
	case time.Sunday:
		premium = weekendSundayPremium
		reason = "weekend:sunday"
	}

	adjustedRate := clamp(rate*premium, cfg.Rate.Min, cfg.Rate.Max)

	return &domain.DecisionResult{
		Offers: []domain.OfferDecision{
			{Amount: amount, Rate: adjustedRate, Period: cfg.Period.Min},
		},
		Reason: reason,
	}
}

// ComputeWeekendMultiplier returns the weekend premium multiplier for the given time.
// Returns 1.0 for weekdays. Used by CompositeStrategy.
func ComputeWeekendMultiplier(t time.Time) float64 {
	t = t.UTC()
	switch t.Weekday() {
	case time.Friday:
		if t.Hour() >= fridayEveningHour {
			return weekendFridayPremium
		}
	case time.Saturday:
		return weekendSaturdayPremium
	case time.Sunday:
		return weekendSundayPremium
	}
	return 1.0
}
