package signal

import (
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestMDC_EmptySignals(t *testing.T) {
	agg := NewMDCAggregator()
	now := time.Now()
	result := agg.Aggregate(nil, nil, now)
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

	result := agg.Aggregate(signals, nil, now)
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
	result := agg.Aggregate(signals, nil, now)
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
	result := agg.Aggregate(signals, nil, now)
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
	freshResult := agg.Aggregate(freshBookSignals, nil, now)

	// Same signals but book is stale → momentum dominates → more negative
	staleBookSignals := []domain.SignalValue{
		{Type: domain.SignalBookConsumption, Value: 0.8, Confidence: 1.0, Timestamp: now.Add(-5 * time.Minute)},
		{Type: domain.SignalMomentum, Value: -0.8, Confidence: 1.0, Timestamp: now},
	}
	staleResult := agg.Aggregate(staleBookSignals, nil, now)

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
	result := agg.Aggregate(signals, nil, now)

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
		{Type: domain.SignalBookConsumption, Value: 0.8, Confidence: 1.0, Timestamp: now}, // demand
		{Type: domain.SignalMomentum, Value: -0.6, Confidence: 1.0, Timestamp: now},       // supply
		{Type: domain.SignalMarginUsage, Value: 0.4, Confidence: 1.0, Timestamp: now},     // demand
	}
	result := agg.Aggregate(signals, nil, now)

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
	result := agg.Aggregate(signals, nil, now)
	// Zero confidence → effective weight = 0 → total weight = 0 → zero result
	if result.Score != 0 {
		t.Errorf("expected zero score for zero-confidence signal, got %f", result.Score)
	}
}

// --- Graceful Degradation (§6.3) ---

func TestMDC_Degradation_SingleSignalExcluded(t *testing.T) {
	agg := NewMDCAggregator()
	now := time.Now()

	signals := []domain.SignalValue{
		{Type: domain.SignalBookConsumption, Value: 0.5, Confidence: 1.0, Timestamp: now},
		{Type: domain.SignalCrossCurrency, Value: 0.3, Confidence: 1.0, Timestamp: now},
		{Type: domain.SignalMarginUsage, Value: 0.2, Confidence: 1.0, Timestamp: now},
	}

	health := domain.SignalHealthSummary{
		domain.SignalBookConsumption: domain.SignalHealthy,
		domain.SignalCrossCurrency:   domain.SignalDegraded, // excluded
		domain.SignalMarginUsage:     domain.SignalHealthy,
	}

	result := agg.Aggregate(signals, health, now)
	// CrossCurrency excluded — should still get a positive score from Book + Margin
	if result.Score <= 0 {
		t.Errorf("expected positive score with one degraded, got %f", result.Score)
	}
}

func TestMDC_Degradation_OrderBookFailure_FRROnly(t *testing.T) {
	agg := NewMDCAggregator()
	now := time.Now()

	signals := []domain.SignalValue{
		{Type: domain.SignalBookConsumption, Value: 0.8, Confidence: 1.0, Timestamp: now},
		{Type: domain.SignalMomentum, Value: 0.5, Confidence: 1.0, Timestamp: now},
	}

	health := domain.SignalHealthSummary{
		domain.SignalBookConsumption: domain.SignalDegraded, // Order Book failure
		domain.SignalMomentum:        domain.SignalHealthy,
	}

	result := agg.Aggregate(signals, health, now)
	if result.Score != 0 {
		t.Errorf("order book failure: expected MDC=0 (FRR-only), got %f", result.Score)
	}
}

func TestMDC_Degradation_LiquidationFailure_BookBoost(t *testing.T) {
	agg := NewMDCAggregator()
	now := time.Now()

	signals := []domain.SignalValue{
		{Type: domain.SignalBookConsumption, Value: 0.6, Confidence: 1.0, Timestamp: now},
		{Type: domain.SignalLiquidationCascade, Value: 0.0, Confidence: 1.0, Timestamp: now},
		{Type: domain.SignalMarginUsage, Value: 0.4, Confidence: 1.0, Timestamp: now},
	}

	health := domain.SignalHealthSummary{
		domain.SignalBookConsumption:    domain.SignalHealthy,
		domain.SignalLiquidationCascade: domain.SignalDegraded,
		domain.SignalMarginUsage:        domain.SignalHealthy,
	}

	result := agg.Aggregate(signals, health, now)
	// Should still produce a result (book at boosted weight)
	if result.Score <= 0 {
		t.Errorf("liquidation failure: expected positive score, got %f", result.Score)
	}
}

func TestMDC_Degradation_RecoveringExcluded(t *testing.T) {
	agg := NewMDCAggregator()
	now := time.Now()

	signals := []domain.SignalValue{
		{Type: domain.SignalBookConsumption, Value: 0.5, Confidence: 1.0, Timestamp: now},
		{Type: domain.SignalMomentum, Value: 0.3, Confidence: 1.0, Timestamp: now},
	}

	health := domain.SignalHealthSummary{
		domain.SignalBookConsumption: domain.SignalHealthy,
		domain.SignalMomentum:        domain.SignalRecovering, // excluded during recovery
	}

	resultWithRecovering := agg.Aggregate(signals, health, now)

	// Compare with both healthy — recovering signal should be excluded
	healthAll := domain.SignalHealthSummary{
		domain.SignalBookConsumption: domain.SignalHealthy,
		domain.SignalMomentum:        domain.SignalHealthy,
	}
	resultAllHealthy := agg.Aggregate(signals, healthAll, now)

	// Scores should differ since momentum is excluded in recovering case
	if resultWithRecovering.Score == resultAllHealthy.Score {
		t.Error("recovering signal should be excluded — scores should differ")
	}
}

func TestMDC_Degradation_NilHealth_BackwardCompat(t *testing.T) {
	agg := NewMDCAggregator()
	now := time.Now()

	signals := []domain.SignalValue{
		{Type: domain.SignalBookConsumption, Value: 0.5, Confidence: 1.0, Timestamp: now},
		{Type: domain.SignalMomentum, Value: -0.3, Confidence: 1.0, Timestamp: now},
	}

	resultNil := agg.Aggregate(signals, nil, now)
	resultEmpty := agg.Aggregate(signals, domain.SignalHealthSummary{}, now)

	if resultNil.Score != resultEmpty.Score {
		t.Errorf("nil vs empty health should be identical: nil=%f, empty=%f", resultNil.Score, resultEmpty.Score)
	}
}

func TestMDC_Degradation_MultipleFailures(t *testing.T) {
	agg := NewMDCAggregator()
	now := time.Now()

	signals := []domain.SignalValue{
		{Type: domain.SignalBookConsumption, Value: 0.6, Confidence: 1.0, Timestamp: now},
		{Type: domain.SignalLiquidationCascade, Value: 0.0, Confidence: 1.0, Timestamp: now},
		{Type: domain.SignalMomentum, Value: 0.3, Confidence: 1.0, Timestamp: now},
		{Type: domain.SignalMarginUsage, Value: 0.4, Confidence: 1.0, Timestamp: now},
		{Type: domain.SignalCrossCurrency, Value: 0.2, Confidence: 1.0, Timestamp: now},
	}

	health := domain.SignalHealthSummary{
		domain.SignalBookConsumption:    domain.SignalHealthy,
		domain.SignalLiquidationCascade: domain.SignalDegraded,
		domain.SignalMomentum:           domain.SignalHealthy,
		domain.SignalMarginUsage:        domain.SignalHealthy,
		domain.SignalCrossCurrency:      domain.SignalDegraded,
	}

	result := agg.Aggregate(signals, health, now)
	if result.Score <= 0 {
		t.Errorf("multiple failures: expected positive score from remaining signals, got %f", result.Score)
	}
}
