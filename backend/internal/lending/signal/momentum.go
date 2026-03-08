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
type Momentum struct {
	trades []vwapTrade
}

type vwapTrade struct {
	rate   float64
	volume float64
	ts     time.Time
}

func NewMomentum() *Momentum {
	return &Momentum{}
}

func (m *Momentum) Name() string { return string(domain.SignalMomentum) }

func (m *Momentum) Compute(data *domain.RawMarketData) domain.SignalValue {
	now := data.Timestamp

	// Append new trades
	for _, t := range data.RecentTrades {
		m.trades = append(m.trades, vwapTrade{
			rate:   t.Rate,
			volume: math.Abs(t.Amount),
			ts:     t.MTS,
		})
	}

	// Prune older than slow window
	cutoff := now.Add(-slowVWAPWindow)
	pruned := m.trades[:0]
	for _, t := range m.trades {
		if t.ts.After(cutoff) {
			pruned = append(pruned, t)
		}
	}
	m.trades = pruned

	if len(m.trades) == 0 {
		return domain.SignalValue{
			Type: domain.SignalMomentum, Value: 0, Confidence: 0, Timestamp: now,
		}
	}

	fastCutoff := now.Add(-fastVWAPWindow)
	fastVWAP := computeVWAP(m.trades, fastCutoff)
	slowVWAP := computeVWAP(m.trades, cutoff)

	if slowVWAP == 0 {
		return domain.SignalValue{
			Type: domain.SignalMomentum, Value: 0, Confidence: 0.3, Timestamp: now,
		}
	}

	// Relative difference
	diff := (fastVWAP - slowVWAP) / slowVWAP
	signal := math.Tanh(diff * 100) // scale and compress

	confidence := math.Min(float64(len(m.trades))/20.0, 1.0)

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
