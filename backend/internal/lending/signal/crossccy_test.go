package signal

import (
	"math"
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestCrossCurrency_NoData(t *testing.T) {
	cc := NewCrossCurrency()
	now := time.Now()
	sv := cc.Compute(&domain.RawMarketData{Timestamp: now})
	if sv.Value != 0 || sv.Confidence != 0 {
		t.Errorf("expected zero for no data, got value=%f conf=%f", sv.Value, sv.Confidence)
	}
}

func TestCrossCurrency_FRRFallback(t *testing.T) {
	cc := NewCrossCurrency()
	now := time.Now()

	// First call with FRR (no trades)
	cc.Compute(&domain.RawMarketData{
		Ticker:    &domain.FundingTicker{FRR: 0.001},
		Timestamp: now,
	})

	// Second call should produce a signal
	sv := cc.Compute(&domain.RawMarketData{
		Ticker:    &domain.FundingTicker{FRR: 0.001},
		Timestamp: now.Add(time.Second),
	})
	// Stable rate → near-zero signal
	if math.Abs(sv.Value) > 0.1 {
		t.Errorf("expected near-zero for stable FRR, got %f", sv.Value)
	}
	if sv.Confidence == 0 {
		t.Error("expected non-zero confidence with 2 data points")
	}
}

func TestCrossCurrency_RisingRate(t *testing.T) {
	cc := NewCrossCurrency()
	now := time.Now()

	// Build history at low rate
	for i := 0; i < 10; i++ {
		cc.Compute(&domain.RawMarketData{
			RecentTrades: []domain.FundingTradeRecord{
				{Rate: 0.0005, Amount: 1000, MTS: now.Add(time.Duration(i) * time.Second)},
			},
			Timestamp: now.Add(time.Duration(i) * time.Second),
		})
	}

	// New data at higher rate
	sv := cc.Compute(&domain.RawMarketData{
		RecentTrades: []domain.FundingTradeRecord{
			{Rate: 0.002, Amount: 1000, MTS: now.Add(11 * time.Second)},
		},
		Timestamp: now.Add(11 * time.Second),
	})

	// Rate rising → positive signal
	if sv.Value <= 0 {
		t.Errorf("expected positive signal for rising rate, got %f", sv.Value)
	}
}

func TestCrossCurrency_FallingRate(t *testing.T) {
	cc := NewCrossCurrency()
	now := time.Now()

	// Build history at high rate
	for i := 0; i < 10; i++ {
		cc.Compute(&domain.RawMarketData{
			RecentTrades: []domain.FundingTradeRecord{
				{Rate: 0.002, Amount: 1000, MTS: now.Add(time.Duration(i) * time.Second)},
			},
			Timestamp: now.Add(time.Duration(i) * time.Second),
		})
	}

	// New data at lower rate
	sv := cc.Compute(&domain.RawMarketData{
		RecentTrades: []domain.FundingTradeRecord{
			{Rate: 0.0005, Amount: 1000, MTS: now.Add(11 * time.Second)},
		},
		Timestamp: now.Add(11 * time.Second),
	})

	// Rate falling → negative signal
	if sv.Value >= 0 {
		t.Errorf("expected negative signal for falling rate, got %f", sv.Value)
	}
}

func TestCrossCurrency_HistoryCap(t *testing.T) {
	cc := NewCrossCurrency()
	now := time.Now()
	for i := 0; i < 30; i++ {
		cc.Compute(&domain.RawMarketData{
			RecentTrades: []domain.FundingTradeRecord{
				{Rate: 0.001, Amount: 1000, MTS: now.Add(time.Duration(i) * time.Second)},
			},
			Timestamp: now.Add(time.Duration(i) * time.Second),
		})
	}
	if len(cc.history) > crossCcyHistorySize {
		t.Errorf("history exceeded cap: %d > %d", len(cc.history), crossCcyHistorySize)
	}
}
