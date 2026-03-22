package signal

import (
	"math"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const marginHistorySize = 20

// MarginUsage estimates demand pressure from bid/ask depth ratio changes.
type MarginUsage struct {
	history []ratioSnapshot
}

type ratioSnapshot struct {
	ts       time.Time
	bidDepth float64
	askDepth float64
}

func NewMarginUsage() *MarginUsage {
	return &MarginUsage{}
}

func (m *MarginUsage) Name() string { return string(domain.SignalMarginUsage) }

func (m *MarginUsage) Compute(data *domain.RawMarketData) domain.SignalValue {
	now := data.Timestamp

	var bidDepth, askDepth float64
	for _, e := range data.Book {
		if e.Amount > 0 {
			askDepth += e.Amount
		} else {
			bidDepth += math.Abs(e.Amount)
		}
	}

	m.history = append(m.history, ratioSnapshot{
		bidDepth: bidDepth, askDepth: askDepth, ts: now,
	})
	if len(m.history) > marginHistorySize {
		m.history = m.history[len(m.history)-marginHistorySize:]
	}

	if len(m.history) < 2 || (bidDepth+askDepth) == 0 {
		return domain.SignalValue{
			Type: domain.SignalMarginUsage, Value: 0, Confidence: 0, Timestamp: now,
		}
	}

	// Current bid/ask ratio
	currentRatio := bidDepth / (bidDepth + askDepth) // 0.5 = balanced

	// Historical average ratio
	var sumRatio float64
	var count int
	for _, h := range m.history {
		total := h.bidDepth + h.askDepth
		if total > 0 {
			sumRatio += h.bidDepth / total
			count++
		}
	}
	avgRatio := sumRatio / float64(count)

	// Signal: deviation from historical average
	// Positive = more bid (borrowing demand) than average
	deviation := currentRatio - avgRatio
	signal := math.Tanh(deviation * 20) // scale and compress

	confidence := math.Min(float64(len(m.history))/float64(marginHistorySize), 1.0)

	return domain.SignalValue{
		Type: domain.SignalMarginUsage, Value: signal, Confidence: confidence, Timestamp: now,
	}
}
