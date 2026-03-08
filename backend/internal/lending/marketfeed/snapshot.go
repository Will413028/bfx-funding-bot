package marketfeed

import (
	"math"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const defaultWallThreshold = 0.05 // 5% of total side depth

func computeOrderBookSummary(entries []domain.BookEntry) domain.OrderBookSummary {
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
			// Offer (lending) side
			askDepth += e.Amount
			if e.Rate < bestAsk {
				bestAsk = e.Rate
				hasAsk = true
			}
		} else if e.Amount < 0 {
			// Bid (borrowing) side
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

func detectWallPositions(entries []domain.BookEntry, threshold float64) []domain.WallPosition {
	if len(entries) == 0 {
		return nil
	}

	if threshold <= 0 {
		threshold = defaultWallThreshold
	}

	// Calculate total depth per side
	var totalOffer, totalBid float64
	for _, e := range entries {
		if e.Amount > 0 {
			totalOffer += e.Amount
		} else {
			totalBid += math.Abs(e.Amount)
		}
	}

	var walls []domain.WallPosition
	for _, e := range entries {
		if e.Amount > 0 && totalOffer > 0 {
			if e.Amount/totalOffer >= threshold {
				walls = append(walls, domain.WallPosition{
					Rate:       e.Rate,
					Amount:     e.Amount,
					Side:       "offer",
					EntryCount: e.Count,
				})
			}
		} else if e.Amount < 0 && totalBid > 0 {
			amt := math.Abs(e.Amount)
			if amt/totalBid >= threshold {
				walls = append(walls, domain.WallPosition{
					Rate:       e.Rate,
					Amount:     amt,
					Side:       "bid",
					EntryCount: e.Count,
				})
			}
		}
	}

	return walls
}

func assembleSnapshot(
	symbol string,
	raw *domain.RawMarketData,
	signals []domain.SignalValue,
	regime domain.RegimeType,
	regimeParams domain.RegimeParams,
	flashFreeze bool,
	now time.Time,
) *domain.MarketSnapshot {
	summary := computeOrderBookSummary(raw.Book)
	walls := detectWallPositions(raw.Book, defaultWallThreshold)

	return &domain.MarketSnapshot{
		Symbol:        symbol,
		Regime:        regime,
		RegimeParams:  regimeParams,
		Signals:       signals,
		OrderBook:     summary,
		WallPositions: walls,
		FlashFreeze:   flashFreeze,
		Timestamp:     now,
	}
}
