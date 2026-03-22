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
	bookGaps := orderbook.DetectGaps(raw.Book, summary.Spread, 0)

	var frr float64
	if raw.Ticker != nil {
		frr = raw.Ticker.FRR
	}

	// G12: Extract FRR trend from signals
	var frrTrend float64
	for _, s := range signals {
		if s.Type == domain.SignalFRRTrend {
			frrTrend = s.Value
			break
		}
	}

	// S4: Extract rate percentile from signals
	var ratePercentile float64
	for _, s := range signals {
		if s.Type == domain.SignalRatePercentile {
			ratePercentile = s.Value
			break
		}
	}

	return &domain.MarketSnapshot{
		Symbol:             symbol,
		FRR:                frr,
		RatePercentile:     ratePercentile,
		FRRTrend:           frrTrend,
		BookGaps:           bookGaps,
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
