package marketfeed

import (
	"sync"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	defaultRateDropThreshold = -0.30 // -30% daily change
	defaultCooldownDuration  = 5 * time.Minute
)

// FlashCrashDetector monitors market data for extreme conditions
// and triggers a freeze when a flash crash is detected.
type FlashCrashDetector struct {
	frozenAt          time.Time
	rateDropThreshold float64
	cooldownDuration  time.Duration
	mu                sync.Mutex
	frozen            bool
}

// FlashCrashConfig holds configuration for the flash crash detector.
type FlashCrashConfig struct {
	RateDropThreshold float64 // e.g. -0.30 for -30%
	CooldownDuration  time.Duration
}

func NewFlashCrashDetector(cfg *FlashCrashConfig) *FlashCrashDetector {
	threshold := defaultRateDropThreshold
	cooldown := defaultCooldownDuration

	if cfg != nil {
		if cfg.RateDropThreshold != 0 {
			threshold = cfg.RateDropThreshold
		}
		if cfg.CooldownDuration > 0 {
			cooldown = cfg.CooldownDuration
		}
	}

	return &FlashCrashDetector{
		rateDropThreshold: threshold,
		cooldownDuration:  cooldown,
	}
}

// Check evaluates the current market data and returns whether trading should be frozen.
func (d *FlashCrashDetector) Check(ticker *domain.FundingTicker, now time.Time) bool {
	if ticker == nil {
		return d.isFrozen(now)
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	// Check if FRR daily change percentage exceeds threshold
	crashDetected := ticker.DailyChangePerc <= d.rateDropThreshold

	if crashDetected && !d.frozen {
		d.frozen = true
		d.frozenAt = now
	}

	if d.frozen {
		// Check cooldown
		if !crashDetected && now.Sub(d.frozenAt) >= d.cooldownDuration {
			d.frozen = false
			return false
		}
		return true
	}

	return false
}

func (d *FlashCrashDetector) isFrozen(now time.Time) bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.frozen && now.Sub(d.frozenAt) >= d.cooldownDuration {
		d.frozen = false
	}
	return d.frozen
}
