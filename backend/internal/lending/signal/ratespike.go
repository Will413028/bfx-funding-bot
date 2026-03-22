package signal

import (
	"math"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	// EMA alpha for ~1hr at 3sec intervals (1200 snapshots) → α ≈ 0.0017
	spikeEMAAlpha = 0.0017
	// Spike threshold: current rate must be this multiple of EMA
	spikeThreshold = 2.0
	// Minimum data points before detecting spikes
	spikeMinSamples = 20
)

// RateSpikeDetector detects non-liquidation rate spikes by comparing
// the current FRR against a 1-hour EMA.
type RateSpikeDetector struct {
	ema1h float64
	count int
}

// NewRateSpikeDetector creates a new rate spike detector.
func NewRateSpikeDetector() *RateSpikeDetector {
	return &RateSpikeDetector{}
}

// Name returns the signal type name.
func (r *RateSpikeDetector) Name() string { return string(domain.SignalRateSpike) }

// Compute checks if the current rate is spiking relative to the 1hr EMA.
func (r *RateSpikeDetector) Compute(data *domain.RawMarketData) domain.SignalValue {
	if data.Ticker == nil {
		return domain.SignalValue{Type: domain.SignalRateSpike, Timestamp: data.Timestamp}
	}

	currentRate := data.Ticker.FRR
	if currentRate <= 0 {
		return domain.SignalValue{Type: domain.SignalRateSpike, Timestamp: data.Timestamp}
	}

	r.count++

	// Initialize EMA
	if r.count == 1 {
		r.ema1h = currentRate
		return domain.SignalValue{Type: domain.SignalRateSpike, Timestamp: data.Timestamp}
	}

	// Update EMA
	r.ema1h = spikeEMAAlpha*currentRate + (1-spikeEMAAlpha)*r.ema1h

	// Not enough data
	if r.count < spikeMinSamples {
		return domain.SignalValue{Type: domain.SignalRateSpike, Timestamp: data.Timestamp}
	}

	if r.ema1h <= 0 {
		return domain.SignalValue{Type: domain.SignalRateSpike, Timestamp: data.Timestamp}
	}

	ratio := currentRate / r.ema1h
	if ratio >= spikeThreshold {
		value := math.Min(ratio-1.0, 1.0) // normalize: 2x→1.0, 1.5x→0.5
		confidence := math.Min(float64(r.count)/float64(spikeMinSamples*5), 1.0)
		return domain.SignalValue{
			Type:       domain.SignalRateSpike,
			Value:      value,
			Confidence: confidence,
			Timestamp:  data.Timestamp,
		}
	}

	return domain.SignalValue{Type: domain.SignalRateSpike, Timestamp: data.Timestamp}
}
