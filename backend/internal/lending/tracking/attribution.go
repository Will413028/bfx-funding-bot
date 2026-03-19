package tracking

import (
	"math"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// AttributeByTag groups performance records by strategy tag and computes
// per-tag alpha statistics. Optionally splits by regime.
func AttributeByTag(records []domain.PerformanceRecord, splitByRegime bool) []domain.TagAlpha {
	type key struct {
		tag    string
		regime domain.RegimeType
	}

	type accum struct {
		sum    float64
		sumSq  float64
		count  int
		regime domain.RegimeType
	}

	groups := make(map[key]*accum)

	for _, r := range records {
		for _, tag := range r.StrategyTags {
			regime := domain.RegimeType("")
			if splitByRegime {
				regime = r.Regime
			}
			k := key{tag: tag, regime: regime}
			acc, ok := groups[k]
			if !ok {
				acc = &accum{regime: regime}
				groups[k] = acc
			}
			acc.sum += r.Alpha
			acc.sumSq += r.Alpha * r.Alpha
			acc.count++
		}
	}

	result := make([]domain.TagAlpha, 0, len(groups))
	for k, acc := range groups {
		mean := acc.sum / float64(acc.count)
		variance := 0.0
		if acc.count > 1 {
			variance = (acc.sumSq/float64(acc.count) - mean*mean)
			variance = math.Max(0, variance) // guard floating point
		}
		result = append(result, domain.TagAlpha{
			Tag:       k.tag,
			MeanAlpha: mean,
			Variance:  variance,
			Count:     acc.count,
			Regime:    acc.regime,
		})
	}

	return result
}
