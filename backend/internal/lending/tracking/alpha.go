package tracking

import (
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// ComputeAlpha returns actual_rate - frr_at_time (§9.1).
func ComputeAlpha(actualRate, frrAtTime float64) float64 {
	return actualRate - frrAtTime
}

// NewPerformanceRecord builds a PerformanceRecord from execution context.
func NewPerformanceRecord(
	userID string,
	actualRate float64,
	snap *domain.MarketSnapshot,
	decision *domain.DecisionResult,
	amount float64,
	period int,
	currency string,
	now time.Time,
) *domain.PerformanceRecord {
	frr := 0.0
	mdcScore := 0.0
	regime := domain.RegimeNeutral

	if snap != nil {
		frr = snap.FRR
		mdcScore = snap.MDC.Score
		regime = snap.Regime
	}

	tags := decision.StrategyTags
	if tags == nil {
		tags = []string{}
	}
	if decision.Reason != "" {
		tags = appendUnique(tags, decision.Reason)
	}

	return &domain.PerformanceRecord{
		UserID:       userID,
		ActualRate:   actualRate,
		FRRAtTime:    frr,
		Alpha:        ComputeAlpha(actualRate, frr),
		MDCScore:     mdcScore,
		Regime:       regime,
		StrategyTags: tags,
		Amount:       amount,
		Period:       period,
		Currency:     currency,
		Timestamp:    now,
	}
}

func appendUnique(tags []string, tag string) []string {
	for _, t := range tags {
		if t == tag {
			return tags
		}
	}
	return append(tags, tag)
}
