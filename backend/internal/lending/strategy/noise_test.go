package strategy

import (
	"math"
	"testing"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// deterministicNoise returns a NoiseStrategy with a fixed rand value.
func deterministicNoise(val float64) *NoiseStrategy {
	return &NoiseStrategy{randFloat: func() float64 { return val }}
}

func TestNoise_RatePerturbationRange(t *testing.T) {
	// Run with multiple fixed rand values, verify rate stays within ±1%
	baseRate := 0.00025
	for _, rv := range []float64{0.0, 0.25, 0.5, 0.75, 1.0 - 1e-10} {
		s := deterministicNoise(rv)
		ctx := testCtx()

		r := s.Apply(ctx)

		rate := r.Offers[0].Rate
		minRate := baseRate * 0.99
		maxRate := baseRate * 1.01
		// Account for psych avoidance shift
		if rate < minRate-psychShift-1e-8 || rate > maxRate+psychShift+1e-8 {
			t.Errorf("rand=%f: rate %f outside expected range [%f, %f]", rv, rate, minRate, maxRate+psychShift)
		}
	}
}

func TestNoise_AmountPerturbationRange(t *testing.T) {
	baseAmount := 5000.0
	for _, rv := range []float64{0.0, 0.25, 0.5, 0.75, 1.0 - 1e-10} {
		s := deterministicNoise(rv)
		ctx := testCtx()

		r := s.Apply(ctx)

		amt := r.Offers[0].Amount
		minAmt := baseAmount * 0.98
		maxAmt := baseAmount * 1.02
		if amt < minAmt-1e-6 || amt > maxAmt+1e-6 {
			t.Errorf("rand=%f: amount %f outside expected range [%f, %f]", rv, amt, minAmt, maxAmt)
		}
	}
}

func TestNoise_PsychAvoidance(t *testing.T) {
	// Rate exactly at 0.0005 → should be shifted
	result := avoidPsychLevel(0.0005)
	if !approxEqual(result, 0.0005+psychShift, 1e-8) {
		t.Errorf("got %f, want %f", result, 0.0005+psychShift)
	}

	// Rate at 0.001 → should be shifted
	result = avoidPsychLevel(0.001)
	if !approxEqual(result, 0.001+psychShift, 1e-8) {
		t.Errorf("got %f, want %f", result, 0.001+psychShift)
	}

	// Rate at 0.00043 → not near multiple, no shift
	result = avoidPsychLevel(0.00043)
	if !approxEqual(result, 0.00043, 1e-8) {
		t.Errorf("got %f, want 0.00043 (no shift)", result)
	}
}

func TestNoise_PsychAvoidanceNearMultiple(t *testing.T) {
	// Rate within tolerance of 0.0005 → shift
	result := avoidPsychLevel(0.000501)
	if math.Abs(result-0.000501) < 1e-8 {
		t.Errorf("rate %f should have been shifted (within tolerance of 0.0005)", result)
	}

	// Rate outside tolerance
	result = avoidPsychLevel(0.000515)
	if !approxEqual(result, 0.000515, 1e-8) {
		t.Errorf("got %f, want 0.000515 (outside tolerance)", result)
	}
}

func TestNoise_RateClampedToMin(t *testing.T) {
	// rand=0.0 → noise = rate × 0.01 × (0-1) = -1% → rate ≈ 0.99×rate
	// With very low FRR near min, noise could push below
	s := deterministicNoise(0.0) // max negative noise
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.000101
		c.Config.Rate.Min = 0.0001
	})

	r := s.Apply(ctx)

	if r.Offers[0].Rate < 0.0001 {
		t.Errorf("rate %f below min 0.0001", r.Offers[0].Rate)
	}
}

func TestNoise_RateClampedToMax(t *testing.T) {
	s := deterministicNoise(1.0 - 1e-10) // max positive noise
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.00499
		c.Config.Rate.Max = 0.005
	})

	r := s.Apply(ctx)

	if r.Offers[0].Rate > 0.005 {
		t.Errorf("rate %f above max 0.005", r.Offers[0].Rate)
	}
}

func TestNoise_AmountClampedToMax(t *testing.T) {
	s := deterministicNoise(1.0 - 1e-10) // max positive noise
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 10000
		c.Config.Amount.Max = 10000
	})

	r := s.Apply(ctx)

	if r.Offers[0].Amount > 10000 {
		t.Errorf("amount %f above max 10000", r.Offers[0].Amount)
	}
}

func TestNoise_ZeroNoise(t *testing.T) {
	// rand=0.5 → noise = rate × 0.01 × (1-1) = 0
	s := deterministicNoise(0.5)
	ctx := testCtx()

	r := s.Apply(ctx)

	if r.Reason != "noise:applied" {
		t.Fatalf("reason: got %s, want noise:applied", r.Reason)
	}
	// Rate should be very close to original (only psych avoidance might shift)
	if !approxEqual(r.Offers[0].Rate, 0.00025, psychShift+1e-8) {
		t.Errorf("rate: got %f, want ~0.00025", r.Offers[0].Rate)
	}
	if !approxEqual(r.Offers[0].Amount, 5000, 1) {
		t.Errorf("amount: got %f, want ~5000", r.Offers[0].Amount)
	}
}

func TestNoise_FlashFreeze(t *testing.T) {
	s := NewNoiseStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FlashFreeze = true
	})

	r := s.Apply(ctx)

	if r.Reason != "flash_freeze" {
		t.Fatalf("reason: got %s, want flash_freeze", r.Reason)
	}
	if len(r.Offers) != 0 {
		t.Errorf("offers: got %d, want 0", len(r.Offers))
	}
}

func TestNoise_InsufficientBalance(t *testing.T) {
	s := NewNoiseStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 10
	})

	r := s.Apply(ctx)

	if r.Reason != "insufficient_balance" {
		t.Fatalf("reason: got %s, want insufficient_balance", r.Reason)
	}
	if len(r.Offers) != 0 {
		t.Errorf("offers: got %d, want 0", len(r.Offers))
	}
}
