package signal

import (
	"math"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const bookHistorySize = 10

// BookConsumption measures order book depletion speed.
type BookConsumption struct {
	history []depthSnapshot
}

type depthSnapshot struct {
	offerDepth float64
	bidDepth   float64
	ts         time.Time
}

func NewBookConsumption() *BookConsumption {
	return &BookConsumption{}
}

func (b *BookConsumption) Name() string { return string(domain.SignalBookConsumption) }

func (b *BookConsumption) Compute(data *domain.RawMarketData) domain.SignalValue {
	now := data.Timestamp

	// Calculate current total depth
	var offerDepth, bidDepth float64
	for _, e := range data.Book {
		if e.Amount > 0 {
			offerDepth += e.Amount
		} else {
			bidDepth += math.Abs(e.Amount)
		}
	}

	b.history = append(b.history, depthSnapshot{
		offerDepth: offerDepth, bidDepth: bidDepth, ts: now,
	})
	if len(b.history) > bookHistorySize {
		b.history = b.history[len(b.history)-bookHistorySize:]
	}

	// Need at least 2 points
	if len(b.history) < 2 {
		return domain.SignalValue{
			Type: domain.SignalBookConsumption, Value: 0, Confidence: 0, Timestamp: now,
		}
	}

	prev := b.history[0]
	curr := b.history[len(b.history)-1]

	elapsed := curr.ts.Sub(prev.ts).Seconds()
	if elapsed <= 0 {
		return domain.SignalValue{
			Type: domain.SignalBookConsumption, Value: 0, Confidence: 0, Timestamp: now,
		}
	}

	// Consumption = depth decrease per second (offer side focus)
	// Positive consumption = offer side shrinking = demand pressure
	prevTotal := prev.offerDepth + prev.bidDepth
	currTotal := curr.offerDepth + curr.bidDepth

	if prevTotal == 0 {
		return domain.SignalValue{
			Type: domain.SignalBookConsumption, Value: 0, Confidence: 0.3, Timestamp: now,
		}
	}

	changeRate := (prevTotal - currTotal) / prevTotal // positive = depth shrinking
	signal := math.Tanh(changeRate * 10)              // scale and compress to [-1,1]

	confidence := math.Min(float64(len(b.history))/float64(bookHistorySize), 1.0)

	return domain.SignalValue{
		Type: domain.SignalBookConsumption, Value: signal, Confidence: confidence, Timestamp: now,
	}
}
