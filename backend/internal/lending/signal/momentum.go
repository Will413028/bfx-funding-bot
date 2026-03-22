package signal

import (
	"math"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	fastVWAPWindow = 5 * time.Minute
	slowVWAPWindow = 20 * time.Minute
)

// Momentum computes dual-speed VWAP difference signal.
// It operates statelessly on the provided RecentTrades buffer.
type Momentum struct{}

type vwapTrade struct {
	ts     time.Time
	rate   float64
	volume float64
}

func NewMomentum() *Momentum {
	return &Momentum{}
}

func (m *Momentum) Name() string { return string(domain.SignalMomentum) }

func (m *Momentum) Compute(data *domain.RawMarketData) domain.SignalValue {
	now := data.Timestamp

	if len(data.RecentTrades) == 0 {
		return domain.SignalValue{
			Type: domain.SignalMomentum, Value: 0, Confidence: 0, Timestamp: now,
		}
	}

	// Convert to vwapTrade slice, filtering to slow window
	cutoff := now.Add(-slowVWAPWindow)
	var trades []vwapTrade
	for _, t := range data.RecentTrades {
		if t.MTS.After(cutoff) {
			trades = append(trades, vwapTrade{
				rate:   t.Rate,
				volume: math.Abs(t.Amount),
				ts:     t.MTS,
			})
		}
	}

	if len(trades) == 0 {
		return domain.SignalValue{
			Type: domain.SignalMomentum, Value: 0, Confidence: 0, Timestamp: now,
		}
	}

	fastCutoff := now.Add(-fastVWAPWindow)
	fastVWAP := computeVWAP(trades, fastCutoff)
	slowVWAP := computeVWAP(trades, cutoff)

	if slowVWAP == 0 {
		return domain.SignalValue{
			Type: domain.SignalMomentum, Value: 0, Confidence: 0.3, Timestamp: now,
		}
	}

	// Relative difference
	diff := (fastVWAP - slowVWAP) / slowVWAP
	signal := math.Tanh(diff * 100) // scale and compress

	confidence := math.Min(float64(len(trades))/20.0, 1.0)

	return domain.SignalValue{
		Type: domain.SignalMomentum, Value: signal, Confidence: confidence, Timestamp: now,
	}
}

func computeVWAP(trades []vwapTrade, since time.Time) float64 {
	var sumRateVol, sumVol float64
	for _, t := range trades {
		if t.ts.After(since) {
			sumRateVol += t.rate * t.volume
			sumVol += t.volume
		}
	}
	if sumVol == 0 {
		return 0
	}
	return sumRateVol / sumVol
}
