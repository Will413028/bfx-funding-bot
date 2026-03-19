package tracking

import (
	"fmt"
	"math"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	// Maximum adjustment per feedback cycle (§9.3, B.6)
	maxAdjustmentPct = 0.10

	// Minimum executions required for meaningful feedback
	minExecutionsForFeedback = 5

	// Variance threshold: high variance → smaller adjustment
	highVarianceThreshold = 0.0001
)

// GenerateAdjustments analyzes per-tag alpha and produces adaptive parameter
// adjustment recommendations (§9.3). Each adjustment is capped at ±10%.
func GenerateAdjustments(tagAlphas []domain.TagAlpha) []domain.AdaptiveAdjustment {
	var adjustments []domain.AdaptiveAdjustment

	for _, ta := range tagAlphas {
		if ta.Count < minExecutionsForFeedback {
			continue // insufficient data
		}

		adj := generateOne(ta)
		if adj != nil {
			adjustments = append(adjustments, *adj)
		}
	}

	return adjustments
}

func generateOne(ta domain.TagAlpha) *domain.AdaptiveAdjustment {
	if ta.MeanAlpha == 0 {
		return nil
	}

	// Determine direction
	direction := "increase"
	if ta.MeanAlpha < 0 {
		direction = "decrease"
	}

	// Base adjustment: 5% for moderate alpha, 10% for strong alpha
	absMean := math.Abs(ta.MeanAlpha)
	pct := 0.05
	if absMean > 0.0001 { // strong signal
		pct = 0.10
	}

	// Reduce adjustment if variance is high (low confidence)
	if ta.Variance > highVarianceThreshold {
		pct *= 0.5
	}

	// Cap at ±10%
	pct = math.Min(pct, maxAdjustmentPct)

	reason := fmt.Sprintf("7d mean alpha=%.6f (n=%d, var=%.8f)", ta.MeanAlpha, ta.Count, ta.Variance)

	return &domain.AdaptiveAdjustment{
		Tag:        ta.Tag,
		Regime:     ta.Regime,
		Direction:  direction,
		Percentage: pct,
		Reason:     reason,
	}
}
