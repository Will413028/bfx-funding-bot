package signal

import (
	"math"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	liquidationWindow         = 5 * time.Minute
	liquidationTradeThreshold = 50000.0 // single trade amount threshold
	liquidationVolumeThreshold = 5000000.0
	regressionInterval        = 5 * time.Minute // per regression step
)

// LiquidationCascade detects cascading liquidation events.
type LiquidationCascade struct {
	triggered     bool
	triggeredAt   time.Time
	currentLevel  float64 // 1.0 → 0.7 → 0.3 → 0.0
	lastLargeTrade time.Time
	window        []tradeRecord
}

type tradeRecord struct {
	amount float64
	ts     time.Time
}

func NewLiquidationCascade() *LiquidationCascade {
	return &LiquidationCascade{}
}

func (l *LiquidationCascade) Name() string { return string(domain.SignalLiquidationCascade) }

func (l *LiquidationCascade) Compute(data *domain.RawMarketData) domain.SignalValue {
	now := data.Timestamp

	// Add large trades to window
	for _, t := range data.RecentTrades {
		if math.Abs(t.Amount) >= liquidationTradeThreshold {
			l.window = append(l.window, tradeRecord{amount: math.Abs(t.Amount), ts: t.MTS})
			l.lastLargeTrade = t.MTS
		}
	}

	// Prune window
	cutoff := now.Add(-liquidationWindow)
	pruned := l.window[:0]
	var totalVolume float64
	for _, tr := range l.window {
		if tr.ts.After(cutoff) {
			pruned = append(pruned, tr)
			totalVolume += tr.amount
		}
	}
	l.window = pruned

	// Check trigger
	if totalVolume >= liquidationVolumeThreshold && !l.triggered {
		l.triggered = true
		l.triggeredAt = now
		l.currentLevel = 1.0
	}

	// Handle regression
	if l.triggered {
		timeSinceLastLarge := now.Sub(l.lastLargeTrade)

		if timeSinceLastLarge >= regressionInterval*3 {
			l.triggered = false
			l.currentLevel = 0
		} else if timeSinceLastLarge >= regressionInterval*2 {
			l.currentLevel = 0.3
		} else if timeSinceLastLarge >= regressionInterval {
			l.currentLevel = 0.7
		} else {
			l.currentLevel = 1.0
		}

		// New large trade during regression → restart
		if totalVolume >= liquidationVolumeThreshold && l.currentLevel < 1.0 {
			l.currentLevel = 1.0
			l.triggeredAt = now
		}
	}

	confidence := 0.5
	if l.currentLevel == 1.0 {
		confidence = 1.0
	} else if l.currentLevel > 0 {
		confidence = 0.8
	}

	return domain.SignalValue{
		Type:       domain.SignalLiquidationCascade,
		Value:      l.currentLevel,
		Confidence: confidence,
		Timestamp:  now,
	}
}
