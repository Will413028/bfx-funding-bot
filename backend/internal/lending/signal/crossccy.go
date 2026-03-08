package signal

import (
	"math"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const crossCcyHistorySize = 20

// CrossCurrency detects funding rate trend using recent trade rate changes.
type CrossCurrency struct {
	history []rateSnapshot
}

type rateSnapshot struct {
	avgRate float64
	ts      time.Time
}

func NewCrossCurrency() *CrossCurrency {
	return &CrossCurrency{}
}

func (c *CrossCurrency) Name() string { return string(domain.SignalCrossCurrency) }

func (c *CrossCurrency) Compute(data *domain.RawMarketData) domain.SignalValue {
	now := data.Timestamp

	// Compute average rate from recent trades
	var sumRate float64
	var count int
	for _, t := range data.RecentTrades {
		sumRate += t.Rate
		count++
	}

	// Also use ticker FRR as fallback
	if count == 0 && data.Ticker != nil {
		sumRate = data.Ticker.FRR
		count = 1
	}

	if count == 0 {
		return domain.SignalValue{
			Type: domain.SignalCrossCurrency, Value: 0, Confidence: 0, Timestamp: now,
		}
	}

	avgRate := sumRate / float64(count)
	c.history = append(c.history, rateSnapshot{avgRate: avgRate, ts: now})
	if len(c.history) > crossCcyHistorySize {
		c.history = c.history[len(c.history)-crossCcyHistorySize:]
	}

	if len(c.history) < 2 {
		return domain.SignalValue{
			Type: domain.SignalCrossCurrency, Value: 0, Confidence: 0, Timestamp: now,
		}
	}

	// Compare current rate to historical average
	var histSum float64
	for _, h := range c.history {
		histSum += h.avgRate
	}
	histAvg := histSum / float64(len(c.history))

	if histAvg == 0 {
		return domain.SignalValue{
			Type: domain.SignalCrossCurrency, Value: 0, Confidence: 0.3, Timestamp: now,
		}
	}

	// Trend: positive = rates rising, negative = rates falling
	trend := (avgRate - histAvg) / histAvg
	signal := math.Tanh(trend * 50) // scale and compress

	confidence := math.Min(float64(len(c.history))/float64(crossCcyHistorySize), 1.0)

	return domain.SignalValue{
		Type: domain.SignalCrossCurrency, Value: signal, Confidence: confidence, Timestamp: now,
	}
}
