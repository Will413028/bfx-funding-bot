package orderbook

import (
	"math"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	defaultHiddenWindow    = 5 * time.Minute
	defaultMinTradeVolume  = 10000.0
)

// HiddenRatioEstimator estimates the hidden order ratio by comparing
// recent trade volume against visible order book depth.
type HiddenRatioEstimator struct {
	trades []hiddenTrade
	window time.Duration
	minVol float64
}

type hiddenTrade struct {
	amount float64
	ts     time.Time
}

func NewHiddenRatioEstimator() *HiddenRatioEstimator {
	return &HiddenRatioEstimator{
		window: defaultHiddenWindow,
		minVol: defaultMinTradeVolume,
	}
}

// Estimate computes the hidden ratio from current book depth and recent trades.
// HiddenRatio = max(0, (TradeVolume - VisibleDepth) / TradeVolume)
func (h *HiddenRatioEstimator) Estimate(book []domain.BookEntry, recentTrades []domain.FundingTradeRecord, now time.Time) float64 {
	// Append new trades
	for _, t := range recentTrades {
		h.trades = append(h.trades, hiddenTrade{
			amount: math.Abs(t.Amount),
			ts:     t.MTS,
		})
	}

	// Prune old trades
	cutoff := now.Add(-h.window)
	pruned := h.trades[:0]
	var tradeVolume float64
	for _, t := range h.trades {
		if t.ts.After(cutoff) {
			pruned = append(pruned, t)
			tradeVolume += t.amount
		}
	}
	h.trades = pruned

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
