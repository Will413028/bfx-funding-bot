package signal

import (
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestPercentile_P75(t *testing.T) {
	p := NewRatePercentile()
	now := time.Now()

	// Feed 1000 samples (every 10th tick sampled -> 100 stored)
	for i := 0; i < 1000; i++ {
		rate := 0.0001 + float64(i%100)*0.000001 // 0.0001 to 0.0002
		data := &domain.RawMarketData{
			Ticker:    &domain.FundingTicker{FRR: rate},
			Timestamp: now.Add(time.Duration(i) * 3 * time.Second),
		}
		p.Compute(data)
	}

	// Rate at P75 (0.000175)
	data := &domain.RawMarketData{
		Ticker:    &domain.FundingTicker{FRR: 0.000175},
		Timestamp: now.Add(1001 * 3 * time.Second),
	}
	result := p.Compute(data)
	if result.Value <= 0 {
		t.Errorf("expected positive value for P75 rate, got %f", result.Value)
	}
}

func TestPercentile_P25(t *testing.T) {
	p := NewRatePercentile()
	now := time.Now()

	for i := 0; i < 1000; i++ {
		rate := 0.0001 + float64(i%100)*0.000001
		data := &domain.RawMarketData{
			Ticker:    &domain.FundingTicker{FRR: rate},
			Timestamp: now.Add(time.Duration(i) * 3 * time.Second),
		}
		p.Compute(data)
	}

	// Rate at P25 (0.000125)
	data := &domain.RawMarketData{
		Ticker:    &domain.FundingTicker{FRR: 0.000125},
		Timestamp: now.Add(1001 * 3 * time.Second),
	}
	result := p.Compute(data)
	if result.Value >= 0 {
		t.Errorf("expected negative value for P25 rate, got %f", result.Value)
	}
}

func TestPercentile_InsufficientData(t *testing.T) {
	p := NewRatePercentile()
	now := time.Now()

	// Only 50 ticks -> 5 samples (< percentileMinSamples=100)
	for i := 0; i < 50; i++ {
		data := &domain.RawMarketData{
			Ticker:    &domain.FundingTicker{FRR: 0.0003},
			Timestamp: now.Add(time.Duration(i) * 3 * time.Second),
		}
		result := p.Compute(data)
		if result.Value != 0 {
			t.Errorf("step %d: expected zero with insufficient data, got %f", i, result.Value)
		}
	}
}
