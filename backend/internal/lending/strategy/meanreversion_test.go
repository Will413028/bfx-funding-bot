package strategy

import (
	"math"
	"testing"
)

func TestPHigherRate_BelowMean(t *testing.T) {
	// Current rate far below EMA -> high probability of increase
	p := PHigherRate(0.0001, 0.0003, 0.0001, 1.0)
	if p < 0.9 {
		t.Errorf("expected P > 0.9 when rate is far below mean, got %f", p)
	}
}

func TestPHigherRate_AtMean(t *testing.T) {
	// Current rate equals EMA -> P ~ 0.5
	p := PHigherRate(0.0003, 0.0003, 0.0001, 1.0)
	if math.Abs(p-0.5) > 0.01 {
		t.Errorf("expected P ~ 0.5 when at mean, got %f", p)
	}
}

func TestPHigherRate_AboveMean(t *testing.T) {
	// Current rate far above EMA -> low probability of increase
	p := PHigherRate(0.0005, 0.0003, 0.0001, 1.0)
	if p > 0.1 {
		t.Errorf("expected P < 0.1 when rate is far above mean, got %f", p)
	}
}

func TestPHigherRate_ZeroVolatility(t *testing.T) {
	p := PHigherRate(0.0003, 0.0003, 0, 1.0)
	if p != 0.5 {
		t.Errorf("expected P = 0.5 with zero volatility, got %f", p)
	}
}

func TestPHigherRate_ZeroWait(t *testing.T) {
	p := PHigherRate(0.0001, 0.0003, 0.0001, 0)
	if p != 0.5 {
		t.Errorf("expected P = 0.5 with zero wait, got %f", p)
	}
}

func TestNormalCDF_Symmetry(t *testing.T) {
	if math.Abs(normalCDF(0)-0.5) > 0.001 {
		t.Errorf("normalCDF(0) should be 0.5, got %f", normalCDF(0))
	}
	if normalCDF(3) < 0.99 {
		t.Errorf("normalCDF(3) should be > 0.99, got %f", normalCDF(3))
	}
	if normalCDF(-3) > 0.01 {
		t.Errorf("normalCDF(-3) should be < 0.01, got %f", normalCDF(-3))
	}
}
