package orderbook

import (
	"math"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	defaultMinTradeVolume = 10000.0
)

// HiddenRatioEstimator estimates the hidden order ratio by comparing
// recent trade volume against visible order book depth.
//
// The estimator is stateless with respect to trades: the caller is responsible
// for maintaining the time-windowed trade buffer and passing the full buffer
// on each call. This avoids double-counting when the caller already accumulates
// trades across ticks (e.g., buildSnapshot's recentTrades buffer).
type HiddenRatioEstimator struct {
	minVol float64
}

func NewHiddenRatioEstimator() *HiddenRatioEstimator {
	return &HiddenRatioEstimator{
		minVol: defaultMinTradeVolume,
	}
}

// Estimate computes the hidden ratio from current book depth and recent trades.
// HiddenRatio = max(0, (TradeVolume - VisibleDepth) / TradeVolume)
//
// recentTrades must be the complete time-windowed trade buffer; the estimator
// uses it directly without internal accumulation.
func (h *HiddenRatioEstimator) Estimate(book []domain.BookEntry, recentTrades []domain.FundingTradeRecord, _ time.Time) float64 {
	// Sum trade volume from the caller-provided window
	var tradeVolume float64
	for _, t := range recentTrades {
		tradeVolume += math.Abs(t.Amount)
	}

	// Below minimum volume → unreliable estimate
	if tradeVolume < h.minVol {
		return 0
	}

	// Calculate visible offer depth
	var visibleDepth float64
	for _, e := range book {
		if e.Amount > 0 {
			visibleDepth += e.Amount
		}
	}

	ratio := (tradeVolume - visibleDepth) / tradeVolume
	return math.Max(0, ratio)
}
