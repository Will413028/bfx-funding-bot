package orderbook

import (
	"math"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// ComputeSummary calculates an OrderBookSummary from raw book entries.
func ComputeSummary(entries []domain.BookEntry) domain.OrderBookSummary {
	if len(entries) == 0 {
		return domain.OrderBookSummary{}
	}

	var (
		bestBid  float64
		bestAsk  = math.MaxFloat64
		bidDepth float64
		askDepth float64
		count    int
		hasBid   bool
		hasAsk   bool
	)

	for _, e := range entries {
		count++
		if e.Amount > 0 {
			askDepth += e.Amount
			if e.Rate < bestAsk {
				bestAsk = e.Rate
				hasAsk = true
			}
		} else if e.Amount < 0 {
			bidDepth += math.Abs(e.Amount)
			if e.Rate > bestBid {
				bestBid = e.Rate
				hasBid = true
			}
		}
	}

	if !hasAsk {
		bestAsk = 0
	}
	if !hasBid {
		bestBid = 0
	}

	var midRate, spread float64
	if hasBid && hasAsk {
		midRate = (bestBid + bestAsk) / 2
		spread = bestAsk - bestBid
	}

	return domain.OrderBookSummary{
		BestBid:    bestBid,
		BestAsk:    bestAsk,
		MidRate:    midRate,
		Spread:     spread,
		BidDepth:   bidDepth,
		AskDepth:   askDepth,
		EntryCount: count,
	}
}
