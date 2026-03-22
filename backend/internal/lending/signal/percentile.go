package signal

import (
	"math"
	"sort"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	// Sample every 10th snapshot to save memory
	// 7 days x 28800 snapshots/day / 10 = 20160 entries
	percentileSampleInterval = 10
	percentileBufferSize     = 20160
	percentileMinSamples     = 100
)

// RatePercentile tracks historical FRR values and computes
// the percentile rank of the current rate.
type RatePercentile struct {
	buffer []float64
	head   int
	count  int
	tick   int // counts snapshots for sampling
}

// NewRatePercentile creates a new percentile tracker.
func NewRatePercentile() *RatePercentile {
	return &RatePercentile{
		buffer: make([]float64, percentileBufferSize),
	}
}

// Name returns the signal type name.
func (r *RatePercentile) Name() string { return string(domain.SignalRatePercentile) }

// Compute updates the buffer and returns the current rate's percentile.
func (r *RatePercentile) Compute(data *domain.RawMarketData) domain.SignalValue {
	if data.Ticker == nil || data.Ticker.FRR <= 0 {
		return domain.SignalValue{Type: domain.SignalRatePercentile, Timestamp: data.Timestamp}
	}

	currentRate := data.Ticker.FRR
	r.tick++

	// Sample every Nth snapshot
	if r.tick%percentileSampleInterval == 0 {
		r.buffer[r.head] = currentRate
		r.head = (r.head + 1) % percentileBufferSize
		if r.count < percentileBufferSize {
			r.count++
		}
	}

	// Not enough data
	if r.count < percentileMinSamples {
		return domain.SignalValue{Type: domain.SignalRatePercentile, Timestamp: data.Timestamp}
	}

	// Compute percentile rank
	pct := r.percentileRank(currentRate)

	// Normalize to [-1, +1]: P75 -> +0.5, P25 -> -0.5, P50 -> 0
	value := (pct - 0.5) * 2.0

	confidence := math.Min(float64(r.count)/float64(percentileBufferSize), 1.0)

	return domain.SignalValue{
		Type:       domain.SignalRatePercentile,
		Value:      value,
		Confidence: confidence,
		Timestamp:  data.Timestamp,
	}
}

// percentileRank returns the fraction of buffer values below the given rate.
func (r *RatePercentile) percentileRank(rate float64) float64 {
	// Copy active portion of buffer for sorting
	n := r.count
	sorted := make([]float64, n)
	if r.count < percentileBufferSize {
		copy(sorted, r.buffer[:n])
	} else {
		// Buffer is full, copy all
		copy(sorted, r.buffer)
	}
	sort.Float64s(sorted)

	// Count values below current rate
	below := sort.SearchFloat64s(sorted, rate)
	return float64(below) / float64(n)
}
