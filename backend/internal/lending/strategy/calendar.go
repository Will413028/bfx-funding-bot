package strategy

import (
	"math"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// Month-end premium schedule: index = days before month end (0 = end day)
var monthEndPremium = [5]float64{
	1.05, // event day
	1.04, // 1 day before
	1.03, // 2 days before
	1.02, // 3 days before
	1.02, // 1 day after (stored at index 4, handled separately)
}

// Quarterly expiry premium schedule
var quarterlyPremium = [5]float64{
	1.10, // event day
	1.07, // 1 day before
	1.05, // 2 days before
	1.03, // 3 days before
	1.03, // 1 day after
}

// CalendarStrategy adjusts rate and period based on known
// periodic market events (month-end, quarterly expiry).
type CalendarStrategy struct {
	now func() time.Time
}

// NewCalendarStrategy creates a new CalendarStrategy using real time.
func NewCalendarStrategy() *CalendarStrategy {
	return &CalendarStrategy{now: time.Now}
}

// Apply evaluates calendar events and returns adjusted rate and period.
func (c *CalendarStrategy) Apply(ctx *domain.DecisionContext) *domain.DecisionResult {
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
		ts = c.now()
	}
	ts = ts.UTC()

	// Evaluate events
	mePremium, meDays := c.monthEndEvent(ts)
	qePremium, qeDays := c.quarterlyEvent(ts)

	// Take the higher premium
	premium := math.Max(mePremium, qePremium)
	bestDays := meDays
	if qePremium > mePremium {
		bestDays = qeDays
	}

	if premium <= 1.0 {
		return &domain.DecisionResult{
			Offers: []domain.OfferDecision{
				{Amount: amount, Rate: rate, Period: cfg.Period.Min},
			},
			Reason: "calendar:none",
		}
	}

	adjustedRate := clamp(rate*premium, cfg.Rate.Min, cfg.Rate.Max)

	// Period shortening: if event is upcoming (bestDays > 0), limit period
	period := cfg.Period.Min
	if bestDays > 0 && bestDays < cfg.Period.Max {
		period = clampInt(bestDays, cfg.Period.Min, cfg.Period.Max)
	}

	reason := "calendar:month_end"
	if qePremium > mePremium {
		reason = "calendar:quarterly"
	}

	return &domain.DecisionResult{
		Offers: []domain.OfferDecision{
			{Amount: amount, Rate: adjustedRate, Period: period},
		},
		Reason: reason,
	}
}

// monthEndEvent returns (premium, daysToEvent) for month-end proximity.
// daysToEvent: >0 means before event, 0 means event day, -1 means day after.
func (c *CalendarStrategy) monthEndEvent(ts time.Time) (float64, int) {
	year, month, day := ts.Date()
	lastDay := daysInMonth(year, month)

	daysToEnd := lastDay - day

	switch {
	case daysToEnd == 0:
		return monthEndPremium[0], 0 // event day
	case daysToEnd >= 1 && daysToEnd <= 3:
		return monthEndPremium[daysToEnd], daysToEnd // 1-3 days before
	}

	// Check if day after month end (first day of next month)
	if day == 1 {
		return monthEndPremium[4], 0 // day after
	}

	return 1.0, -1
}

// quarterlyEvent returns (premium, daysToEvent) for quarterly expiry proximity.
func (c *CalendarStrategy) quarterlyEvent(ts time.Time) (float64, int) {
	year, month, _ := ts.Date()

	// Only March, June, September, December
	quarterMonth := nearestQuarterMonth(month)
	quarterYear := year
	if quarterMonth < int(month) {
		// Next quarter
		quarterMonth += 3
		if quarterMonth > 12 {
			quarterMonth = 3
			quarterYear++
		}
	}

	lastFriday := lastFridayOf(quarterYear, time.Month(quarterMonth))
	today := time.Date(year, month, ts.Day(), 0, 0, 0, 0, time.UTC)
	eventDay := time.Date(quarterYear, time.Month(quarterMonth), lastFriday, 0, 0, 0, 0, time.UTC)

	diff := int(eventDay.Sub(today).Hours() / 24)

	switch {
	case diff == 0:
		return quarterlyPremium[0], 0
	case diff >= 1 && diff <= 3:
		return quarterlyPremium[diff], diff
	case diff == -1:
		return quarterlyPremium[4], 0
	}

	return 1.0, -1
}

// nearestQuarterMonth returns the nearest quarter-end month (3,6,9,12).
func nearestQuarterMonth(m time.Month) int {
	switch {
	case m <= 3:
		return 3
	case m <= 6:
		return 6
	case m <= 9:
		return 9
	default:
		return 12
	}
}

// lastFridayOf returns the day of the last Friday in the given month.
func lastFridayOf(year int, month time.Month) int {
	lastDay := daysInMonth(year, month)
	t := time.Date(year, month, lastDay, 0, 0, 0, 0, time.UTC)
	for t.Weekday() != time.Friday {
		t = t.AddDate(0, 0, -1)
	}
	return t.Day()
}

// daysInMonth returns the number of days in the given month.
func daysInMonth(year int, month time.Month) int {
	return time.Date(year, month+1, 0, 0, 0, 0, 0, time.UTC).Day()
}

// ComputeCalendarMultiplier returns (premium multiplier, max period days) for
// calendar events at the given time. maxPeriod <= 0 means no period constraint.
// Used by CompositeStrategy.
func ComputeCalendarMultiplier(t time.Time) (multiplier float64, maxPeriod int) {
	c := &CalendarStrategy{now: func() time.Time { return t }}
	t = t.UTC()

	mePremium, meDays := c.monthEndEvent(t)
	qePremium, qeDays := c.quarterlyEvent(t)

	premium := math.Max(mePremium, qePremium)
	bestDays := meDays
	if qePremium > mePremium {
		bestDays = qeDays
	}

	if premium <= 1.0 {
		return 1.0, 0
	}
	return premium, bestDays
}
