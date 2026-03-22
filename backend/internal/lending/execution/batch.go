package execution

import (
	"sort"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// CreditBatch represents a group of credits merged for batch renewal.
type CreditBatch struct {
	ExpiryDate time.Time
	CreditIDs  []int64
	Amount     float64
	Rate       float64
	Period     int
}

// BatchCredits groups credits by expiry date (UTC day) and merges them.
// It computes weighted-average rate, max period, applies config bounds,
// and splits batches exceeding config.Amount.Max.
func BatchCredits(credits []domain.FundingCredit, config *domain.StrategyConfig, now time.Time) []CreditBatch {
	if len(credits) == 0 {
		return nil
	}

	// Group by expiry day
	type group struct {
		day       time.Time
		credits   []domain.FundingCredit
		totalAmt  float64
		weightedR float64 // sum of amount*rate
		maxPeriod int
	}

	groups := make(map[time.Time]*group)
	var dayOrder []time.Time

	for _, c := range credits {
		expiresAt := c.OpenedAt.AddDate(0, 0, c.Period)
		day := expiresAt.Truncate(24 * time.Hour)

		g, ok := groups[day]
		if !ok {
			g = &group{day: day}
			groups[day] = g
			dayOrder = append(dayOrder, day)
		}

		g.credits = append(g.credits, c)
		g.totalAmt += c.Amount
		g.weightedR += c.Amount * c.Rate
		if c.Period > g.maxPeriod {
			g.maxPeriod = c.Period
		}
	}

	// Sort days chronologically
	sort.Slice(dayOrder, func(i, j int) bool { return dayOrder[i].Before(dayOrder[j]) })

	var batches []CreditBatch

	for _, day := range dayOrder {
		g := groups[day]

		// Weighted average rate
		if g.totalAmt == 0 {
			continue
		}
		rate := g.weightedR / g.totalAmt

		// Apply config bounds
		if rate < config.Rate.Min {
			rate = config.Rate.Min
		}
		if rate > config.Rate.Max {
			rate = config.Rate.Max
		}

		period := g.maxPeriod
		if period < config.Period.Min {
			period = config.Period.Min
		}
		if period > config.Period.Max {
			period = config.Period.Max
		}

		// Collect credit IDs
		ids := make([]int64, len(g.credits))
		for i, c := range g.credits {
			ids[i] = c.ID
		}

		// Split if exceeds Amount.Max
		if config.Amount.Max > 0 && g.totalAmt > config.Amount.Max {
			remaining := g.totalAmt
			for remaining > 0 {
				amt := remaining
				if amt > config.Amount.Max {
					amt = config.Amount.Max
				}
				batches = append(batches, CreditBatch{
					ExpiryDate: day,
					Amount:     amt,
					Rate:       rate,
					Period:     period,
					CreditIDs:  ids, // shared reference, all sub-batches track same IDs
				})
				remaining -= amt
			}
		} else {
			batches = append(batches, CreditBatch{
				ExpiryDate: day,
				Amount:     g.totalAmt,
				Rate:       rate,
				Period:     period,
				CreditIDs:  ids,
			})
		}
	}

	return batches
}
