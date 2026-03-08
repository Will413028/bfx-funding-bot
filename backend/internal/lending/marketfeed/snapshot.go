package marketfeed

import (
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/lending/orderbook"
)

func assembleSnapshot(
	symbol string,
	raw *domain.RawMarketData,
	signals []domain.SignalValue,
	regime domain.RegimeType,
	regimeParams domain.RegimeParams,
	flashFreeze bool,
	hiddenRatio float64,
	competitorActivity float64,
	now time.Time,
) *domain.MarketSnapshot {
	summary := orderbook.ComputeSummary(raw.Book)
	walls := orderbook.DetectWalls(raw.Book, summary.Spread, nil)

	return &domain.MarketSnapshot{
		Symbol:             symbol,
		Regime:             regime,
		RegimeParams:       regimeParams,
		Signals:            signals,
		OrderBook:          summary,
		WallPositions:      walls,
		HiddenRatio:        hiddenRatio,
		CompetitorActivity: competitorActivity,
		FlashFreeze:        flashFreeze,
		Timestamp:          now,
	}
}
