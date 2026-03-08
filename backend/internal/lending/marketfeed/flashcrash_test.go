package marketfeed

import (
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestFlashCrashDetector_Normal(t *testing.T) {
	d := NewFlashCrashDetector(nil)
	now := time.Now()

	ticker := &domain.FundingTicker{
		DailyChangePerc: -0.05, // -5%, normal
	}

	if d.Check(ticker, now) {
		t.Error("expected no freeze for normal market")
	}
}

func TestFlashCrashDetector_CrashDetected(t *testing.T) {
	d := NewFlashCrashDetector(nil)
	now := time.Now()

	ticker := &domain.FundingTicker{
		DailyChangePerc: -0.35, // -35%, crash
	}

	if !d.Check(ticker, now) {
		t.Error("expected freeze for crash")
	}
}

func TestFlashCrashDetector_CooldownActive(t *testing.T) {
	d := NewFlashCrashDetector(&FlashCrashConfig{
		CooldownDuration: 5 * time.Minute,
	})
	now := time.Now()

	// Trigger crash
	crash := &domain.FundingTicker{DailyChangePerc: -0.40}
	d.Check(crash, now)

	// 2 minutes later, indicators normal — should still be frozen (cooldown)
	normal := &domain.FundingTicker{DailyChangePerc: -0.05}
	if !d.Check(normal, now.Add(2*time.Minute)) {
		t.Error("expected freeze during cooldown")
	}
}

func TestFlashCrashDetector_CooldownExpired(t *testing.T) {
	d := NewFlashCrashDetector(&FlashCrashConfig{
		CooldownDuration: 5 * time.Minute,
	})
	now := time.Now()

	// Trigger crash
	crash := &domain.FundingTicker{DailyChangePerc: -0.40}
	d.Check(crash, now)

	// 6 minutes later, indicators normal — should unfreeze
	normal := &domain.FundingTicker{DailyChangePerc: -0.05}
	if d.Check(normal, now.Add(6*time.Minute)) {
		t.Error("expected unfreeze after cooldown expired")
	}
}

func TestFlashCrashDetector_CustomThreshold(t *testing.T) {
	d := NewFlashCrashDetector(&FlashCrashConfig{
		RateDropThreshold: -0.10, // stricter: -10%
	})
	now := time.Now()

	ticker := &domain.FundingTicker{DailyChangePerc: -0.15}
	if !d.Check(ticker, now) {
		t.Error("expected freeze with custom threshold -10%")
	}
}

func TestFlashCrashDetector_NilTicker(t *testing.T) {
	d := NewFlashCrashDetector(nil)
	now := time.Now()

	// No ticker, not frozen
	if d.Check(nil, now) {
		t.Error("expected no freeze with nil ticker")
	}

	// Trigger crash first, then nil ticker — should remain frozen
	crash := &domain.FundingTicker{DailyChangePerc: -0.40}
	d.Check(crash, now)
	if !d.Check(nil, now.Add(1*time.Minute)) {
		t.Error("expected still frozen with nil ticker during cooldown")
	}
}
