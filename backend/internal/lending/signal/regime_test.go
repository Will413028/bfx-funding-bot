package signal

import (
	"math"
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestRegime_Contango(t *testing.T) {
	rd := NewRegimeDetector(nil)
	now := time.Now()
	mdc := domain.MDCResult{Score: 0.5, DemandPressure: 0.4, SupplyPressure: 0.1}
	ticker := &domain.FundingTicker{DailyChangePerc: 0.05}

	regime, _ := rd.Detect(mdc, ticker, false, now)
	if regime != domain.RegimeContango {
		t.Errorf("expected contango, got %s", regime)
	}
}

func TestRegime_Backwardation(t *testing.T) {
	rd := NewRegimeDetector(nil)
	now := time.Now()
	mdc := domain.MDCResult{Score: -0.4, DemandPressure: 0.05, SupplyPressure: 0.3}
	ticker := &domain.FundingTicker{DailyChangePerc: -0.03}

	regime, _ := rd.Detect(mdc, ticker, false, now)
	if regime != domain.RegimeBackwardation {
		t.Errorf("expected backwardation, got %s", regime)
	}
}

func TestRegime_Neutral(t *testing.T) {
	rd := NewRegimeDetector(nil)
	now := time.Now()
	mdc := domain.MDCResult{Score: 0.1, DemandPressure: 0.1, SupplyPressure: 0.1}
	ticker := &domain.FundingTicker{DailyChangePerc: 0.01}

	regime, _ := rd.Detect(mdc, ticker, false, now)
	if regime != domain.RegimeNeutral {
		t.Errorf("expected neutral, got %s", regime)
	}
}

func TestRegime_CrisisFromFlashFreeze(t *testing.T) {
	rd := NewRegimeDetector(nil)
	now := time.Now()
	mdc := domain.MDCResult{Score: 0.1} // mild score
	ticker := &domain.FundingTicker{DailyChangePerc: -0.05}

	regime, _ := rd.Detect(mdc, ticker, true, now)
	if regime != domain.RegimeCrisis {
		t.Errorf("expected crisis from flash freeze, got %s", regime)
	}
}

func TestRegime_CrisisFromExtremeScore(t *testing.T) {
	rd := NewRegimeDetector(nil)
	now := time.Now()
	ticker := &domain.FundingTicker{DailyChangePerc: -0.30}

	// Warm up volatility EMA so smoothedVol exceeds crisis threshold
	for i := 0; i < 10; i++ {
		rd.Detect(domain.MDCResult{Score: 0.5}, ticker, false, now.Add(time.Duration(i)*time.Second))
	}

	mdc := domain.MDCResult{Score: 0.95}
	regime, _ := rd.Detect(mdc, ticker, false, now.Add(11*time.Second))
	if regime != domain.RegimeCrisis {
		t.Errorf("expected crisis from extreme score + high vol, got %s", regime)
	}
}

func TestRegime_HysteresisStay(t *testing.T) {
	rd := NewRegimeDetector(nil)
	now := time.Now()
	ticker := &domain.FundingTicker{DailyChangePerc: 0.05}

	// Enter contango
	rd.Detect(domain.MDCResult{Score: 0.35}, ticker, false, now)

	// Score drops to 0.25 (between exit=0.2 and enter=0.3) → stay contango
	regime, _ := rd.Detect(domain.MDCResult{Score: 0.25}, ticker, false, now.Add(time.Minute))
	if regime != domain.RegimeContango {
		t.Errorf("expected contango (hysteresis), got %s", regime)
	}
}

func TestRegime_HysteresisExit(t *testing.T) {
	rd := NewRegimeDetector(nil)
	now := time.Now()
	ticker := &domain.FundingTicker{DailyChangePerc: 0.05}

	// Enter contango
	rd.Detect(domain.MDCResult{Score: 0.35}, ticker, false, now)

	// Score drops below exit threshold → neutral
	regime, _ := rd.Detect(domain.MDCResult{Score: 0.15}, ticker, false, now.Add(time.Minute))
	if regime != domain.RegimeNeutral {
		t.Errorf("expected neutral after exit threshold, got %s", regime)
	}
}

func TestRegime_HysteresisBackwardation(t *testing.T) {
	rd := NewRegimeDetector(nil)
	now := time.Now()
	ticker := &domain.FundingTicker{DailyChangePerc: -0.05}

	// Enter backwardation
	rd.Detect(domain.MDCResult{Score: -0.35}, ticker, false, now)

	// Score rises to -0.25 → stay backwardation (hysteresis)
	regime, _ := rd.Detect(domain.MDCResult{Score: -0.25}, ticker, false, now.Add(time.Minute))
	if regime != domain.RegimeBackwardation {
		t.Errorf("expected backwardation (hysteresis), got %s", regime)
	}

	// Score rises above -0.2 → neutral
	regime, _ = rd.Detect(domain.MDCResult{Score: -0.15}, ticker, false, now.Add(2*time.Minute))
	if regime != domain.RegimeNeutral {
		t.Errorf("expected neutral, got %s", regime)
	}
}

func TestRegime_ColdStart(t *testing.T) {
	rd := NewRegimeDetector(nil)
	now := time.Now()

	regime, params := rd.Detect(domain.MDCResult{}, nil, false, now)
	if regime != domain.RegimeNeutral {
		t.Errorf("expected neutral on cold start, got %s", regime)
	}
	if params.Duration != 0 {
		t.Errorf("expected zero duration on first call, got %v", params.Duration)
	}
}

func TestRegime_DurationTracking(t *testing.T) {
	rd := NewRegimeDetector(nil)
	now := time.Now()
	ticker := &domain.FundingTicker{DailyChangePerc: 0.05}

	// Enter contango at t=0
	rd.Detect(domain.MDCResult{Score: 0.5}, ticker, false, now)

	// Check duration at t=10min
	_, params := rd.Detect(domain.MDCResult{Score: 0.5}, ticker, false, now.Add(10*time.Minute))
	expected := 10 * time.Minute
	if math.Abs(params.Duration.Seconds()-expected.Seconds()) > 1 {
		t.Errorf("expected ~10min duration, got %v", params.Duration)
	}
}

func TestRegime_Params(t *testing.T) {
	rd := NewRegimeDetector(nil)
	now := time.Now()
	mdc := domain.MDCResult{Score: 0.6, DemandPressure: 0.5, SupplyPressure: 0.1}
	ticker := &domain.FundingTicker{DailyChangePerc: 0.08}

	_, params := rd.Detect(mdc, ticker, false, now)

	if params.TrendStrength != 0.6 {
		t.Errorf("TrendStrength: got %f, want 0.6", params.TrendStrength)
	}
	expectedRatio := 0.5 / 0.1
	if math.Abs(params.DemandSupplyRatio-expectedRatio) > 0.01 {
		t.Errorf("DemandSupplyRatio: got %f, want %f", params.DemandSupplyRatio, expectedRatio)
	}
	if params.Volatility == 0 {
		t.Error("expected non-zero volatility")
	}
}

func TestRegime_CryptoThresholds(t *testing.T) {
	// Crypto preset has lower enter threshold (0.25 vs 0.40),
	// so a score of 0.30 should trigger contango for crypto but not stablecoin.
	cryptoDet := NewRegimeDetectorWithPreset(domain.CryptoPreset)
	stableDet := NewRegimeDetector(nil)

	now := time.Now()
	mdc := domain.MDCResult{Score: 0.30, DemandPressure: 0.3, SupplyPressure: 0.1, Timestamp: now}

	cryptoRegime, _ := cryptoDet.Detect(mdc, nil, false, now)
	stableRegime, _ := stableDet.Detect(mdc, nil, false, now)

	if cryptoRegime != domain.RegimeContango {
		t.Errorf("crypto with score 0.30 should be contango (enter=0.25), got %s", cryptoRegime)
	}
	if stableRegime != domain.RegimeNeutral {
		t.Errorf("stablecoin with score 0.30 should be neutral (enter=0.40), got %s", stableRegime)
	}
}
