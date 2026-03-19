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
	domain.SignalIntraday:           0.10,
}

// Default decay λ per signal (per second).
// Higher λ = faster decay = signal must be fresher to matter.
var defaultLambda = map[domain.SignalType]float64{
	domain.SignalBookConsumption:    0.01,
	domain.SignalLiquidationCascade: 0.005,
	domain.SignalMarginUsage:        0.008,
	domain.SignalMomentum:           0.01,
	domain.SignalCrossCurrency:      0.005,
	domain.SignalIntraday:           0.002,
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
// health is optional (nil = backward compatible, all signals treated as healthy).
func (m *MDCAggregator) Aggregate(signals []domain.SignalValue, health domain.SignalHealthSummary, now time.Time) domain.MDCResult {
	if len(signals) == 0 {
		return domain.MDCResult{Score: 0, DemandPressure: 0, SupplyPressure: 0, Timestamp: now}
	}

	// Check for liquidation hard override (only if liquidation is not degraded)
	for _, s := range signals {
		if s.Type == domain.SignalLiquidationCascade && s.Value > 0 {
			if !isExcluded(s.Type, health) {
				return domain.MDCResult{
					Score:          s.Value,
					DemandPressure: s.Value,
					SupplyPressure: 0,
					Timestamp:      now,
				}
			}
		}
	}

	// Order Book failure → FRR-only mode (MDC = 0)
	if isExcluded(domain.SignalBookConsumption, health) {
		return domain.MDCResult{Score: 0, DemandPressure: 0, SupplyPressure: 0, Timestamp: now}
	}

	// Build effective weights with degradation overrides
	effectiveWeights := m.buildDegradedWeights(health)

	// Compute weighted sum with freshness decay
	var totalWeight float64
	weightedSum := 0.0
	var demandSum, supplySum float64

	for _, s := range signals {
		baseWeight, ok := effectiveWeights[s.Type]
		if !ok || baseWeight == 0 {
			continue
		}
		lambda, ok := m.lambda[s.Type]
		if !ok {
			lambda = 0.01
		}

		age := now.Sub(s.Timestamp).Seconds()
		if age < 0 {
			age = 0
		}

		effectiveWeight := baseWeight * s.Confidence * math.Exp(-lambda*age)
		totalWeight += effectiveWeight
		weightedSum += s.Value * effectiveWeight

		if s.Value > 0 {
			demandSum += s.Value * effectiveWeight
		} else {
			supplySum += math.Abs(s.Value) * effectiveWeight
		}
	}

	if totalWeight == 0 {
		return domain.MDCResult{Score: 0, DemandPressure: 0, SupplyPressure: 0, Timestamp: now}
	}

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

// isExcluded returns true if the signal should be excluded from MDC calculation.
func isExcluded(sigType domain.SignalType, health domain.SignalHealthSummary) bool {
	if health == nil {
		return false
	}
	state, ok := health[sigType]
	if !ok {
		return false
	}
	return state == domain.SignalDegraded || state == domain.SignalRecovering
}

// buildDegradedWeights applies degradation rules and returns adjusted weights.
// Returns a new map with excluded signals zeroed and remaining weights renormalized.
func (m *MDCAggregator) buildDegradedWeights(health domain.SignalHealthSummary) map[domain.SignalType]float64 {
	if health == nil {
		return m.weights
	}

	weights := make(map[domain.SignalType]float64, len(m.weights))
	for k, v := range m.weights {
		weights[k] = v
	}

	// Apply specific degradation rules before zeroing
	if isExcluded(domain.SignalLiquidationCascade, health) {
		// Liquidation failure → boost BookConsumption to 35%
		weights[domain.SignalBookConsumption] = 0.35
	}

	// Zero out excluded signals
	var removedWeight float64
	for sigType := range weights {
		if isExcluded(sigType, health) {
			removedWeight += weights[sigType]
			weights[sigType] = 0
		}
	}

	if removedWeight == 0 {
		return weights
	}

	// Special case: momentum failure → redistribute to margin + crosscurrency only
	if isExcluded(domain.SignalMomentum, health) && !isExcluded(domain.SignalMarginUsage, health) && !isExcluded(domain.SignalCrossCurrency, health) {
		// Already zeroed momentum; redistribute momentum's weight proportionally to margin + cross
		marginBase := weights[domain.SignalMarginUsage]
		crossBase := weights[domain.SignalCrossCurrency]
		sumTarget := marginBase + crossBase
		if sumTarget > 0 {
			momentumWeight := m.weights[domain.SignalMomentum]
			weights[domain.SignalMarginUsage] += momentumWeight * (marginBase / sumTarget)
			weights[domain.SignalCrossCurrency] += momentumWeight * (crossBase / sumTarget)
		}
	}

	// Renormalize remaining weights to sum to 1.0
	var total float64
	for _, w := range weights {
		total += w
	}
	if total > 0 && total != 1.0 {
		for k, w := range weights {
			if w > 0 {
				weights[k] = w / total
			}
		}
	}

	return weights
}
