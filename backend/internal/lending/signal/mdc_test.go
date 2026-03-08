package signal

import (
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestMDC_EmptySignals(t *testing.T) {
	agg := NewMDCAggregator()
	now := time.Now()
	result := agg.Aggregate(nil, now)
	if result.Score != 0 || result.DemandPressure != 0 || result.SupplyPressure != 0 {
		t.Errorf("expected zero result for empty signals, got %+v", result)
	}
}

func TestMDC_LiquidationHardOverride(t *testing.T) {
	agg := NewMDCAggregator()
	now := time.Now()

	signals := []domain.SignalValue{
		{Type: domain.SignalBookConsumption, Value: -0.5, Confidence: 1.0, Timestamp: now},
		{Type: domain.SignalLiquidationCascade, Value: 1.0, Confidence: 1.0, Timestamp: now},
		{Type: domain.SignalMomentum, Value: -0.8, Confidence: 1.0, Timestamp: now},
	}

	result := agg.Aggregate(signals, now)
	if result.Score != 1.0 {
		t.Errorf("expected hard override score=1.0, got %f", result.Score)
	}
	if result.DemandPressure != 1.0 {
		t.Errorf("expected demand=1.0, got %f", result.DemandPressure)
	}
}

func TestMDC_LiquidationRegression(t *testing.T) {
	agg := NewMDCAggregator()
	now := time.Now()

	// Liquidation in regression state (0.7)
	signals := []domain.SignalValue{
		{Type: domain.SignalLiquidationCascade, Value: 0.7, Confidence: 0.8, Timestamp: now},
	}
	result := agg.Aggregate(signals, now)
	if result.Score != 0.7 {
		t.Errorf("expected score=0.7 during regression, got %f", result.Score)
	}
}

func TestMDC_LiquidationZeroNoOverride(t *testing.T) {
	agg := NewMDCAggregator()
	now := time.Now()

	// Liquidation with value=0 should NOT trigger override
	signals := []domain.SignalValue{
		{Type: domain.SignalBookConsumption, Value: 0.5, Confidence: 1.0, Timestamp: now},
		{Type: domain.SignalLiquidationCascade, Value: 0, Confidence: 0.5, Timestamp: now},
	}
	result := agg.Aggregate(signals, now)
	if result.Score == 0 {
		t.Error("expected non-zero score when liquidation is inactive")
	}
}

func TestMDC_FreshnessDecay(t *testing.T) {
	agg := NewMDCAggregator()
	now := time.Now()

	// Two signals: book (positive) and momentum (negative)
	// When book is fresh, its weight dominates → positive score
	freshBookSignals := []domain.SignalValue{
		{Type: domain.SignalBookConsumption, Value: 0.8, Confidence: 1.0, Timestamp: now},
		{Type: domain.SignalMomentum, Value: -0.8, Confidence: 1.0, Timestamp: now},
	}
	freshResult := agg.Aggregate(freshBookSignals, now)

	// Same signals but book is stale → momentum dominates → more negative
	staleBookSignals := []domain.SignalValue{
		{Type: domain.SignalBookConsumption, Value: 0.8, Confidence: 1.0, Timestamp: now.Add(-5 * time.Minute)},
		{Type: domain.SignalMomentum, Value: -0.8, Confidence: 1.0, Timestamp: now},
	}
	staleResult := agg.Aggregate(staleBookSignals, now)

	// Fresh book should pull score more positive than stale book
	if freshResult.Score <= staleResult.Score {
		t.Errorf("fresh book score (%f) should be > stale book score (%f)", freshResult.Score, staleResult.Score)
	}
}

func TestMDC_WeightedAggregation(t *testing.T) {
	agg := NewMDCAggregator()
	now := time.Now()

	signals := []domain.SignalValue{
		{Type: domain.SignalBookConsumption, Value: 0.5, Confidence: 1.0, Timestamp: now},
		{Type: domain.SignalMomentum, Value: -0.5, Confidence: 1.0, Timestamp: now},
		{Type: domain.SignalMarginUsage, Value: 0.3, Confidence: 1.0, Timestamp: now},
		{Type: domain.SignalCrossCurrency, Value: 0.0, Confidence: 1.0, Timestamp: now},
	}
	result := agg.Aggregate(signals, now)

	// Book (0.25×0.5) + Momentum (0.15×-0.5) + Margin (0.20×0.3) + CrossCcy (0.10×0)
	// = 0.125 - 0.075 + 0.06 + 0 = 0.11
	// Normalized: 0.11 / (0.25+0.15+0.20+0.10) = 0.11 / 0.70 ≈ 0.157
	// tanh(0.157) ≈ 0.156
	if result.Score <= 0 {
		t.Errorf("expected positive composite score, got %f", result.Score)
	}
}

func TestMDC_DemandSupplyDecomposition(t *testing.T) {
	agg := NewMDCAggregator()
	now := time.Now()

	signals := []domain.SignalValue{
		{Type: domain.SignalBookConsumption, Value: 0.8, Confidence: 1.0, Timestamp: now},    // demand
		{Type: domain.SignalMomentum, Value: -0.6, Confidence: 1.0, Timestamp: now},           // supply
		{Type: domain.SignalMarginUsage, Value: 0.4, Confidence: 1.0, Timestamp: now},         // demand
	}
	result := agg.Aggregate(signals, now)

	if result.DemandPressure <= 0 {
		t.Errorf("expected positive demand pressure, got %f", result.DemandPressure)
	}
	if result.SupplyPressure <= 0 {
		t.Errorf("expected positive supply pressure, got %f", result.SupplyPressure)
	}
}

func TestMDC_WeightsSumToOne(t *testing.T) {
	var total float64
	for _, w := range defaultWeights {
		total += w
	}
	if total < 0.999 || total > 1.001 {
		t.Errorf("expected weights to sum to 1.0, got %f", total)
	}
	if len(defaultWeights) != 6 {
		t.Errorf("expected 6 signal weights, got %d", len(defaultWeights))
	}
}

func TestMDC_AllSignalsHaveDecay(t *testing.T) {
	for sig := range defaultWeights {
		if _, ok := defaultLambda[sig]; !ok {
			t.Errorf("signal %s has weight but no decay lambda", sig)
		}
	}
}

func TestMDC_ZeroConfidenceIgnored(t *testing.T) {
	agg := NewMDCAggregator()
	now := time.Now()

	signals := []domain.SignalValue{
		{Type: domain.SignalBookConsumption, Value: 0.9, Confidence: 0, Timestamp: now},
	}
	result := agg.Aggregate(signals, now)
	// Zero confidence → effective weight = 0 → total weight = 0 → zero result
	if result.Score != 0 {
		t.Errorf("expected zero score for zero-confidence signal, got %f", result.Score)
	}
}
