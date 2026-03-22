package signal

import (
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestRateSpike_Triggered(t *testing.T) {
	r := NewRateSpikeDetector()
	now := time.Now()

	// Build up EMA with normal rates
	for i := 0; i < 30; i++ {
		data := &domain.RawMarketData{
			Ticker:    &domain.FundingTicker{FRR: 0.0003},
			Timestamp: now.Add(time.Duration(i) * 3 * time.Second),
		}
		r.Compute(data)
	}

	// Spike: rate well above 2x EMA (EMA updates before check, so needs to exceed threshold after update)
	data := &domain.RawMarketData{
		Ticker:    &domain.FundingTicker{FRR: 0.0007},
		Timestamp: now.Add(30 * 3 * time.Second),
	}
	result := r.Compute(data)
	if result.Value <= 0 {
		t.Errorf("expected positive spike signal, got %f", result.Value)
	}
}

func TestRateSpike_NormalFluctuation(t *testing.T) {
	r := NewRateSpikeDetector()
	now := time.Now()

	for i := 0; i < 30; i++ {
		data := &domain.RawMarketData{
			Ticker:    &domain.FundingTicker{FRR: 0.0003},
			Timestamp: now.Add(time.Duration(i) * 3 * time.Second),
		}
		r.Compute(data)
	}

	// Small increase: 17% above EMA — should NOT trigger
	data := &domain.RawMarketData{
		Ticker:    &domain.FundingTicker{FRR: 0.00035},
		Timestamp: now.Add(30 * 3 * time.Second),
	}
	result := r.Compute(data)
	if result.Value > 0 {
		t.Errorf("expected no spike for normal fluctuation, got %f", result.Value)
	}
}

func TestRateSpike_InsufficientData(t *testing.T) {
	r := NewRateSpikeDetector()
	now := time.Now()

	for i := 0; i < spikeMinSamples-1; i++ {
		data := &domain.RawMarketData{
			Ticker:    &domain.FundingTicker{FRR: 0.0003},
			Timestamp: now.Add(time.Duration(i) * 3 * time.Second),
		}
		result := r.Compute(data)
		if result.Value != 0 {
			t.Errorf("step %d: expected zero with insufficient data, got %f", i, result.Value)
		}
	}
}
