package orderbook

import (
	"math"
	"sort"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const defaultDustFactor = 0.05

// DustOptions configures the dust filter.
type DustOptions struct {
	Factor float64 // multiplier applied to median (default 0.05)
}

// FilterDust removes dust entries below a dynamic threshold based on
// the median amount per side. Each side (offer/bid) is filtered independently.
func FilterDust(entries []domain.BookEntry, opts *DustOptions) []domain.BookEntry {
	if len(entries) == 0 {
		return nil
	}

	factor := defaultDustFactor
	if opts != nil && opts.Factor > 0 {
		factor = opts.Factor
	}

	// Separate amounts by side
	var offerAmounts, bidAmounts []float64
	for _, e := range entries {
		if e.Amount > 0 {
			offerAmounts = append(offerAmounts, e.Amount)
		} else if e.Amount < 0 {
			bidAmounts = append(bidAmounts, math.Abs(e.Amount))
		}
	}

	offerThreshold := median(offerAmounts) * factor
	bidThreshold := median(bidAmounts) * factor

	filtered := make([]domain.BookEntry, 0, len(entries))
	for _, e := range entries {
		if e.Amount > 0 {
			if e.Amount >= offerThreshold {
				filtered = append(filtered, e)
			}
		} else if e.Amount < 0 {
			if math.Abs(e.Amount) >= bidThreshold {
				filtered = append(filtered, e)
			}
		}
	}

	return filtered
}

func median(vals []float64) float64 {
	if len(vals) == 0 {
		return 0
	}
	sorted := make([]float64, len(vals))
	copy(sorted, vals)
	sort.Float64s(sorted)

	n := len(sorted)
	if n%2 == 0 {
		return (sorted[n/2-1] + sorted[n/2]) / 2
	}
	return sorted[n/2]
}
