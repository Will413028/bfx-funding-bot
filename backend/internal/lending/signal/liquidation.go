package signal

import (
	"math"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	liquidationTradeThreshold  = 50000.0 // single trade amount threshold
	liquidationVolumeThreshold = 5000000.0
	regressionInterval         = 5 * time.Minute // per regression step

	// Cascade phase durations
	cascadeEarlyDuration = 30 * time.Minute
	cascadeMidDuration   = 2 * time.Hour
)

// LiquidationCascade detects cascading liquidation events.
// It computes large-trade volume directly from the provided RecentTrades buffer
// instead of accumulating trades internally.
type LiquidationCascade struct {
	triggeredAt    time.Time
	lastLargeTrade time.Time
	currentLevel   float64
	triggered      bool
}

func NewLiquidationCascade() *LiquidationCascade {
	return &LiquidationCascade{}
}

func (l *LiquidationCascade) Name() string { return string(domain.SignalLiquidationCascade) }

func (l *LiquidationCascade) Compute(data *domain.RawMarketData) domain.SignalValue {
	now := data.Timestamp

	// Compute large-trade volume directly from the provided buffer
	var totalVolume float64
	for _, t := range data.RecentTrades {
		amt := math.Abs(t.Amount)
		if amt >= liquidationTradeThreshold {
			totalVolume += amt
			if t.MTS.After(l.lastLargeTrade) {
				l.lastLargeTrade = t.MTS
			}
		}
	}

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

// CascadePhase returns the current phase of the liquidation cascade.
func (l *LiquidationCascade) CascadePhase(now time.Time) string {
	if !l.triggered {
		return "none"
	}
	elapsed := now.Sub(l.triggeredAt)
	switch {
	case elapsed < cascadeEarlyDuration:
		return "early"
	case elapsed < cascadeMidDuration:
		return "mid"
	default:
		return "late"
	}
}
