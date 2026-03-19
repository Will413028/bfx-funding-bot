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
	mdc domain.MDCResult,
	regime domain.RegimeType,
	regimeParams domain.RegimeParams,
	flashFreeze bool,
	hiddenRatio float64,
	competitorActivity float64,
	signalHealth domain.SignalHealthSummary,
	degradedMode bool,
	degradedReason string,
	now time.Time,
) *domain.MarketSnapshot {
	summary := orderbook.ComputeSummary(raw.Book)
	walls := orderbook.DetectWalls(raw.Book, summary.Spread, nil)

	var frr float64
	if raw.Ticker != nil {
		frr = raw.Ticker.FRR
	}

	return &domain.MarketSnapshot{
		Symbol:             symbol,
		FRR:                frr,
		MDC:                mdc,
		Regime:             regime,
		RegimeParams:       regimeParams,
		Signals:            signals,
		OrderBook:          summary,
		WallPositions:      walls,
		HiddenRatio:        hiddenRatio,
		CompetitorActivity: competitorActivity,
		FlashFreeze:        flashFreeze,
		SignalHealth:       signalHealth,
		DegradedMode:       degradedMode,
		DegradedReason:     degradedReason,
		Timestamp:          now,
	}
}
