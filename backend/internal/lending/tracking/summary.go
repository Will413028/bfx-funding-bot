package tracking

import (
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// Summarize computes a rolling AlphaSummary from a set of PerformanceRecords
// within the given time window.
func Summarize(records []domain.PerformanceRecord, since time.Time) domain.AlphaSummary {
	var totalAlpha float64
	var count int
	regimeMap := make(map[domain.RegimeType]*regimeAccum)

	for _, r := range records {
		if r.Timestamp.Before(since) {
			continue
		}
		totalAlpha += r.Alpha
		count++

		acc, ok := regimeMap[r.Regime]
		if !ok {
			acc = &regimeAccum{}
			regimeMap[r.Regime] = acc
		}
		acc.total += r.Alpha
		acc.count++
	}

	summary := domain.AlphaSummary{
		TotalAlpha: totalAlpha,
		Count:      count,
		ByRegime:   make(map[domain.RegimeType]domain.RegimeAlpha),
	}

	if count > 0 {
		summary.MeanAlpha = totalAlpha / float64(count)
	}

	for regime, acc := range regimeMap {
		ra := domain.RegimeAlpha{Count: acc.count}
		if acc.count > 0 {
			ra.MeanAlpha = acc.total / float64(acc.count)
		}
		summary.ByRegime[regime] = ra
	}

	return summary
}

type regimeAccum struct {
	total float64
	count int
}
