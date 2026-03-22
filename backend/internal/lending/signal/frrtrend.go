package signal

import (
	"math"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	// EMA alphas: α = 2/(N+1) where N = number of snapshots
	// Short EMA ~30min at 3sec intervals = 600 snapshots → α ≈ 0.0033
	frrShortAlpha = 0.0033
	// Long EMA ~4hr at 3sec intervals = 4800 snapshots → α ≈ 0.0004
	frrLongAlpha = 0.0004
	// Minimum samples before outputting a trend
	frrMinSamples = 10
	// Scaling factor for trend normalization
	frrTrendScale = 10.0
)

// FRRTrend tracks short and long term EMA of FRR to detect rate trends.
type FRRTrend struct {
	shortEMA float64
	longEMA  float64
	count    int
}

// NewFRRTrend creates a new FRR trend tracker.
func NewFRRTrend() *FRRTrend {
	return &FRRTrend{}
}

// Name returns the signal type name.
func (f *FRRTrend) Name() string { return string(domain.SignalFRRTrend) }

// Compute updates EMAs with the current FRR and returns the trend signal.
func (f *FRRTrend) Compute(data *domain.RawMarketData) domain.SignalValue {
	if data.Ticker == nil {
		return domain.SignalValue{Type: domain.SignalFRRTrend, Timestamp: data.Timestamp}
	}

	frr := data.Ticker.FRR
	if frr <= 0 {
		return domain.SignalValue{Type: domain.SignalFRRTrend, Timestamp: data.Timestamp}
	}

	f.count++

	// Initialize EMAs on first data point
	if f.count == 1 {
		f.shortEMA = frr
		f.longEMA = frr
		return domain.SignalValue{Type: domain.SignalFRRTrend, Timestamp: data.Timestamp}
	}

	// Update EMAs
	f.shortEMA = frrShortAlpha*frr + (1-frrShortAlpha)*f.shortEMA
	f.longEMA = frrLongAlpha*frr + (1-frrLongAlpha)*f.longEMA

	// Not enough data yet
	if f.count < frrMinSamples {
		return domain.SignalValue{Type: domain.SignalFRRTrend, Timestamp: data.Timestamp}
	}

	// Trend = (shortEMA - longEMA) / longEMA, compressed with tanh
	if f.longEMA <= 0 {
		return domain.SignalValue{Type: domain.SignalFRRTrend, Timestamp: data.Timestamp}
	}

	trend := (f.shortEMA - f.longEMA) / f.longEMA
	value := math.Tanh(trend * frrTrendScale) // normalize to [-1, +1]

	confidence := math.Min(float64(f.count)/100.0, 1.0)

	return domain.SignalValue{
		Type:       domain.SignalFRRTrend,
		Value:      value,
		Confidence: confidence,
		Timestamp:  data.Timestamp,
	}
}
