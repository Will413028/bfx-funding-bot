package signal

import (
	"math"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	defaultEnterThreshold    = 0.3
	defaultExitThreshold     = 0.2
	defaultCrisisScoreThresh = 0.9
	defaultCrisisVolThresh   = 0.20 // 20% daily change
	volatilityEMAAlpha       = 0.3
	minSupplyPressure        = 0.001
)

// RegimeConfig configures the regime detector thresholds.
type RegimeConfig struct {
	EnterThreshold float64 // MDC score to enter contango/backwardation (default 0.3)
	ExitThreshold  float64 // MDC score to exit back to neutral (default 0.2)
}

// RegimeDetector identifies the current market regime from MDC results
// and market data, with hysteresis to prevent rapid switching.
type RegimeDetector struct {
	regimeStart   time.Time
	currentRegime domain.RegimeType
	enterThresh   float64
	exitThresh    float64
	smoothedVol   float64
	initialized   bool
}

func NewRegimeDetector(cfg *RegimeConfig) *RegimeDetector {
	enter := defaultEnterThreshold
	exit := defaultExitThreshold
	if cfg != nil {
		if cfg.EnterThreshold > 0 {
			enter = cfg.EnterThreshold
		}
		if cfg.ExitThreshold > 0 {
			exit = cfg.ExitThreshold
		}
	}
	return &RegimeDetector{
		enterThresh:   enter,
		exitThresh:    exit,
		currentRegime: domain.RegimeNeutral,
	}
}

// Detect determines the current market regime and computes regime parameters.
func (r *RegimeDetector) Detect(
	mdc domain.MDCResult,
	ticker *domain.FundingTicker,
	flashFreeze bool,
	now time.Time,
) (domain.RegimeType, domain.RegimeParams) {
	if !r.initialized {
		r.initialized = true
		r.regimeStart = now
	}

	// Update smoothed volatility
	rawVol := 0.0
	if ticker != nil {
		rawVol = math.Abs(ticker.DailyChangePerc)
	}
	r.smoothedVol = volatilityEMAAlpha*rawVol + (1-volatilityEMAAlpha)*r.smoothedVol

	// Determine new regime
	newRegime := r.classify(mdc.Score, flashFreeze)

	// Track regime transitions
	if newRegime != r.currentRegime {
		r.currentRegime = newRegime
		r.regimeStart = now
	}

	// Compute params
	params := domain.RegimeParams{
		Volatility:        r.smoothedVol,
		TrendStrength:     mdc.Score,
		DemandSupplyRatio: mdc.DemandPressure / math.Max(mdc.SupplyPressure, minSupplyPressure),
		Duration:          now.Sub(r.regimeStart),
	}

	return r.currentRegime, params
}

func (r *RegimeDetector) classify(score float64, flashFreeze bool) domain.RegimeType {
	// Crisis takes priority
	if flashFreeze {
		return domain.RegimeCrisis
	}
	if math.Abs(score) > defaultCrisisScoreThresh && r.smoothedVol > defaultCrisisVolThresh {
		return domain.RegimeCrisis
	}

	// Apply hysteresis
	switch r.currentRegime {
	case domain.RegimeContango:
		if score < r.exitThresh {
			return r.classifyFresh(score)
		}
		return domain.RegimeContango

	case domain.RegimeBackwardation:
		if score > -r.exitThresh {
			return r.classifyFresh(score)
		}
		return domain.RegimeBackwardation

	case domain.RegimeCrisis:
		// Exit crisis when flash freeze is off and score is moderate
		if !flashFreeze && math.Abs(score) <= defaultCrisisScoreThresh {
			return r.classifyFresh(score)
		}
		return domain.RegimeCrisis

	default: // neutral
		return r.classifyFresh(score)
	}
}

func (r *RegimeDetector) classifyFresh(score float64) domain.RegimeType {
	if score > r.enterThresh {
		return domain.RegimeContango
	}
	if score < -r.enterThresh {
		return domain.RegimeBackwardation
	}
	return domain.RegimeNeutral
}
