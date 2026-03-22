package orderbook

import (
	"sort"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// DetectGaps scans ask-side entries for rate gaps wider than minGapWidth.
// If minGapWidth <= 0, defaults to spread * 3.
func DetectGaps(entries []domain.BookEntry, spread float64, minGapWidth float64) []domain.RateGap {
	if minGapWidth <= 0 {
		minGapWidth = spread * 3
	}
	if minGapWidth <= 0 {
		return nil
	}

	// Collect ask-side rates (Amount > 0 = offer/lending)
	var askRates []float64
	for _, e := range entries {
		if e.Amount > 0 && e.Rate > 0 {
			askRates = append(askRates, e.Rate)
		}
	}

	if len(askRates) < 2 {
		return nil
	}

	sort.Float64s(askRates)

	// Deduplicate
	unique := askRates[:1]
	for i := 1; i < len(askRates); i++ {
		if askRates[i] != unique[len(unique)-1] {
			unique = append(unique, askRates[i])
		}
	}

	if len(unique) < 2 {
		return nil
	}

	var gaps []domain.RateGap
	for i := 1; i < len(unique); i++ {
		width := unique[i] - unique[i-1]
		if width >= minGapWidth {
			gaps = append(gaps, domain.RateGap{
				Low:   unique[i-1],
				High:  unique[i],
				Width: width,
			})
		}
	}

	return gaps
}

// FindGapForRate returns the gap that contains the given rate, or nil if none.
func FindGapForRate(gaps []domain.RateGap, rate float64) *domain.RateGap {
	for i := range gaps {
		if rate > gaps[i].Low && rate < gaps[i].High {
			return &gaps[i]
		}
	}
	return nil
}
