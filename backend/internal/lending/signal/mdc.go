package signal

import (
	"math"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// Default base weights for each signal source.
var defaultWeights = map[domain.SignalType]float64{
	domain.SignalBookConsumption:    0.25,
	domain.SignalLiquidationCascade: 0.20,
	domain.SignalMarginUsage:        0.20,
	domain.SignalMomentum:           0.15,
	domain.SignalCrossCurrency:      0.10,
	// Remaining 10% reserved for future intraday signal
}

// Default decay λ per signal (per second).
// Higher λ = faster decay = signal must be fresher to matter.
var defaultLambda = map[domain.SignalType]float64{
	domain.SignalBookConsumption:    0.01,
	domain.SignalLiquidationCascade: 0.005,
	domain.SignalMarginUsage:        0.008,
	domain.SignalMomentum:           0.01,
	domain.SignalCrossCurrency:      0.005,
}

// MDCAggregator computes the Market Demand Curve composite score
// from multiple signal sources using freshness-weighted aggregation.
type MDCAggregator struct {
	weights map[domain.SignalType]float64
	lambda  map[domain.SignalType]float64
}

func NewMDCAggregator() *MDCAggregator {
	return &MDCAggregator{
		weights: defaultWeights,
		lambda:  defaultLambda,
	}
}

// Aggregate computes the MDC result from a set of signal values.
func (m *MDCAggregator) Aggregate(signals []domain.SignalValue, now time.Time) domain.MDCResult {
	if len(signals) == 0 {
		return domain.MDCResult{Score: 0, DemandPressure: 0, SupplyPressure: 0, Timestamp: now}
	}

	// Check for liquidation hard override
	for _, s := range signals {
		if s.Type == domain.SignalLiquidationCascade && s.Value > 0 {
			// Liquidation active → hard override MDC to the liquidation level
			return domain.MDCResult{
				Score:          s.Value, // 1.0, 0.7, or 0.3 from regression
				DemandPressure: s.Value,
				SupplyPressure: 0,
				Timestamp:      now,
			}
		}
	}

	// Compute effective weights with freshness decay
	var totalWeight float64
	weightedSum := 0.0
	var demandSum, supplySum float64

	for _, s := range signals {
		baseWeight, ok := m.weights[s.Type]
		if !ok {
			continue
		}
		lambda, ok := m.lambda[s.Type]
		if !ok {
			lambda = 0.01 // fallback
		}

		age := now.Sub(s.Timestamp).Seconds()
		if age < 0 {
			age = 0
		}

		// EffectiveWeight = BaseWeight × Confidence × e^(-λ × age)
		effectiveWeight := baseWeight * s.Confidence * math.Exp(-lambda*age)
		totalWeight += effectiveWeight
		weightedSum += s.Value * effectiveWeight

		// Demand/supply pressure decomposition
		if s.Value > 0 {
			demandSum += s.Value * effectiveWeight
		} else {
			supplySum += math.Abs(s.Value) * effectiveWeight
		}
	}

	if totalWeight == 0 {
		return domain.MDCResult{Score: 0, DemandPressure: 0, SupplyPressure: 0, Timestamp: now}
	}

	// Renormalize and compress
	normalized := weightedSum / totalWeight
	score := math.Tanh(normalized)

	demandPressure := demandSum / totalWeight
	supplyPressure := supplySum / totalWeight

	return domain.MDCResult{
		Score:          score,
		DemandPressure: demandPressure,
		SupplyPressure: supplyPressure,
		Timestamp:      now,
	}
}
