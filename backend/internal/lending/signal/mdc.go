package signal

import (
	"math"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// MDCAggregator computes the Market Demand Curve composite score
// from multiple signal sources using freshness-weighted aggregation.
type MDCAggregator struct {
	weights map[domain.SignalType]float64
	lambda  map[domain.SignalType]float64
}

// NewMDCAggregatorWithPreset creates a MDCAggregator using a CurrencyPreset.
func NewMDCAggregatorWithPreset(preset domain.CurrencyPreset) *MDCAggregator {
	return &MDCAggregator{
		weights: preset.MDCWeights,
		lambda:  preset.MDCLambda,
	}
}

// NewMDCAggregator creates a MDCAggregator with stablecoin defaults (backward compatible).
func NewMDCAggregator() *MDCAggregator {
	return NewMDCAggregatorWithPreset(domain.StablecoinPreset)
}

// Aggregate computes the MDC result from a set of signal values.
// health is optional (nil = backward compatible, all signals treated as healthy).
// recoveryWeights maps signal types to their recovery dampening factor (0.0–1.0).
// Recovering signals with a weight are included (not excluded) with dampened weight.
func (m *MDCAggregator) Aggregate(signals []domain.SignalValue, health domain.SignalHealthSummary, now time.Time, recoveryWeights ...map[domain.SignalType]float64) domain.MDCResult {
	if len(signals) == 0 {
		return domain.MDCResult{Score: 0, DemandPressure: 0, SupplyPressure: 0, Timestamp: now}
	}

	// Merge recovery weights from variadic arg
	var recWeights map[domain.SignalType]float64
	if len(recoveryWeights) > 0 {
		recWeights = recoveryWeights[0]
	}

	// Check for liquidation hard override (only if liquidation is not degraded)
	for _, s := range signals {
		if s.Type == domain.SignalLiquidationCascade && s.Value > 0 {
			if !isFullyExcluded(s.Type, health, recWeights) {
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
	if isFullyExcluded(domain.SignalBookConsumption, health, recWeights) {
		return domain.MDCResult{Score: 0, DemandPressure: 0, SupplyPressure: 0, Timestamp: now}
	}

	// Build effective weights with degradation overrides
	effectiveWeights := m.buildDegradedWeights(health, recWeights)

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

// isFullyExcluded returns true if the signal should be completely excluded from MDC.
// Degraded signals are always excluded. Recovering signals are excluded only if
// no recovery weight is provided (backward compat with G9-only behavior).
func isFullyExcluded(sigType domain.SignalType, health domain.SignalHealthSummary, recWeights map[domain.SignalType]float64) bool {
	if health == nil {
		return false
	}
	state, ok := health[sigType]
	if !ok {
		return false
	}
	if state == domain.SignalDegraded {
		return true
	}
	if state == domain.SignalRecovering {
		// If recovery weights provided, include with dampened weight
		if recWeights != nil {
			if w, ok := recWeights[sigType]; ok && w > 0 {
				return false
			}
		}
		return true // no recovery weight → exclude (backward compat)
	}
	return false
}

// buildDegradedWeights applies degradation rules and returns adjusted weights.
// Returns a new map with excluded signals zeroed and remaining weights renormalized.
// Recovering signals with recovery weights get dampened instead of zeroed.
func (m *MDCAggregator) buildDegradedWeights(health domain.SignalHealthSummary, recWeights map[domain.SignalType]float64) map[domain.SignalType]float64 {
	if health == nil {
		return m.weights
	}

	weights := make(map[domain.SignalType]float64, len(m.weights))
	for k, v := range m.weights {
		weights[k] = v
	}

	// Apply specific degradation rules before zeroing
	if isFullyExcluded(domain.SignalLiquidationCascade, health, recWeights) {
		// Liquidation failure → boost BookConsumption to 35%
		weights[domain.SignalBookConsumption] = 0.35
	}

	// Zero out excluded signals, apply recovery dampening for recovering ones
	var removedWeight float64
	for sigType := range weights {
		if isFullyExcluded(sigType, health, recWeights) {
			removedWeight += weights[sigType]
			weights[sigType] = 0
		} else if health[sigType] == domain.SignalRecovering && recWeights != nil {
			if rw, ok := recWeights[sigType]; ok && rw < 1.0 {
				dampened := weights[sigType] * rw
				removedWeight += weights[sigType] - dampened
				weights[sigType] = dampened
			}
		}
	}

	if removedWeight == 0 {
		return weights
	}

	// Special case: momentum failure → redistribute to margin + crosscurrency only
	if isFullyExcluded(domain.SignalMomentum, health, recWeights) && !isFullyExcluded(domain.SignalMarginUsage, health, recWeights) && !isFullyExcluded(domain.SignalCrossCurrency, health, recWeights) {
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
