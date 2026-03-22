package signal

import (
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestFRRTrend_RisingRates(t *testing.T) {
	f := NewFRRTrend()
	now := time.Now()

	// Feed increasing FRR values
	for i := 0; i < 20; i++ {
		frr := 0.0003 + float64(i)*0.00001 // rising
		data := &domain.RawMarketData{
			Ticker:    &domain.FundingTicker{FRR: frr},
			Timestamp: now.Add(time.Duration(i) * 3 * time.Second),
		}
		result := f.Compute(data)
		if i >= frrMinSamples && result.Value <= 0 {
			t.Errorf("step %d: expected positive trend for rising rates, got %f", i, result.Value)
		}
	}
}

func TestFRRTrend_FallingRates(t *testing.T) {
	f := NewFRRTrend()
	now := time.Now()

	for i := 0; i < 20; i++ {
		frr := 0.0005 - float64(i)*0.00001 // falling
		data := &domain.RawMarketData{
			Ticker:    &domain.FundingTicker{FRR: frr},
			Timestamp: now.Add(time.Duration(i) * 3 * time.Second),
		}
		result := f.Compute(data)
		if i >= frrMinSamples && result.Value >= 0 {
			t.Errorf("step %d: expected negative trend for falling rates, got %f", i, result.Value)
		}
	}
}

func TestFRRTrend_InsufficientData(t *testing.T) {
	f := NewFRRTrend()
	now := time.Now()

	for i := 0; i < frrMinSamples-1; i++ {
		data := &domain.RawMarketData{
			Ticker:    &domain.FundingTicker{FRR: 0.0003},
			Timestamp: now.Add(time.Duration(i) * 3 * time.Second),
		}
		result := f.Compute(data)
		if result.Value != 0 {
			t.Errorf("step %d: expected zero trend with insufficient data, got %f", i, result.Value)
		}
		if result.Confidence != 0 {
			t.Errorf("step %d: expected zero confidence with insufficient data, got %f", i, result.Confidence)
		}
	}
}
