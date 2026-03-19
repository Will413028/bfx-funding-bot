package strategy

import (
	"math"
	"testing"
)

func TestEarlyReturn_HighHoldRate(t *testing.T) {
	a := NewEarlyReturnAdjuster()
	if w := a.AdjustPeriodWeight(0.80); w != 1.0 {
		t.Errorf("80%% hold rate: expected 1.0, got %f", w)
	}
}

func TestEarlyReturn_ExactThreshold(t *testing.T) {
	a := NewEarlyReturnAdjuster()
	if w := a.AdjustPeriodWeight(0.60); w != 1.0 {
		t.Errorf("60%% hold rate: expected 1.0, got %f", w)
	}
}

func TestEarlyReturn_BelowThreshold(t *testing.T) {
	a := NewEarlyReturnAdjuster()
	w := a.AdjustPeriodWeight(0.30)
	// 0.5 + 0.5 * (0.30/0.60) = 0.5 + 0.25 = 0.75
	if math.Abs(w-0.75) > 0.01 {
		t.Errorf("30%% hold rate: expected ~0.75, got %f", w)
	}
}

func TestEarlyReturn_ZeroHoldRate(t *testing.T) {
	a := NewEarlyReturnAdjuster()
	if w := a.AdjustPeriodWeight(0.0); w != 0.50 {
		t.Errorf("0%% hold rate: expected 0.50, got %f", w)
	}
}

func TestEarlyReturn_EffectiveReturn(t *testing.T) {
	a := NewEarlyReturnAdjuster()
	eff := a.EffectiveReturn(0.001, 0.50) // 50% hold rate
	if math.Abs(eff-0.0005) > 1e-10 {
		t.Errorf("expected 0.0005, got %f", eff)
	}
}
