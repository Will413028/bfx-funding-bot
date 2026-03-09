package signal

import (
	"math"
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestMomentum_NoTrades(t *testing.T) {
	m := NewMomentum()
	now := time.Now()
	sv := m.Compute(&domain.RawMarketData{Timestamp: now})
	if sv.Value != 0 || sv.Confidence != 0 {
		t.Errorf("expected zero for no trades, got value=%f conf=%f", sv.Value, sv.Confidence)
	}
}

func TestMomentum_FlatRate(t *testing.T) {
	m := NewMomentum()
	now := time.Now()

	// Build a full trade buffer with constant rate across the slow window
	trades := make([]domain.FundingTradeRecord, 0, 21)
	for i := 0; i < 20; i++ {
		ts := now.Add(-20*time.Minute + time.Duration(i)*time.Minute)
		trades = append(trades, domain.FundingTradeRecord{
			Rate: 0.001, Amount: 1000, MTS: ts,
		})
	}
	trades = append(trades, domain.FundingTradeRecord{
		Rate: 0.001, Amount: 1000, MTS: now,
	})

	sv := m.Compute(&domain.RawMarketData{
		RecentTrades: trades,
		Timestamp:    now,
	})
	// Fast VWAP ≈ Slow VWAP → signal ≈ 0
	if math.Abs(sv.Value) > 0.1 {
		t.Errorf("expected near-zero signal for flat rate, got %f", sv.Value)
	}
}

func TestMomentum_RisingRate(t *testing.T) {
	m := NewMomentum()
	now := time.Now()

	// Build a buffer with old low-rate trades and recent high-rate trades
	var trades []domain.FundingTradeRecord
	for i := 0; i < 10; i++ {
		ts := now.Add(-15*time.Minute + time.Duration(i)*time.Minute)
		trades = append(trades, domain.FundingTradeRecord{
			Rate: 0.0005, Amount: 1000, MTS: ts,
		})
	}
	for i := 0; i < 5; i++ {
		ts := now.Add(-4*time.Minute + time.Duration(i)*time.Minute)
		trades = append(trades, domain.FundingTradeRecord{
			Rate: 0.002, Amount: 1000, MTS: ts,
		})
	}
	trades = append(trades, domain.FundingTradeRecord{
		Rate: 0.002, Amount: 1000, MTS: now,
	})

	sv := m.Compute(&domain.RawMarketData{
		RecentTrades: trades,
		Timestamp:    now,
	})
	// Fast VWAP > Slow VWAP → positive signal
	if sv.Value <= 0 {
		t.Errorf("expected positive signal for rising rate, got %f", sv.Value)
	}
}

func TestMomentum_OldTradesPruned(t *testing.T) {
	m := NewMomentum()
	now := time.Now()

	// Buffer with one old trade beyond slow window and one recent trade
	trades := []domain.FundingTradeRecord{
		{Rate: 0.001, Amount: 1000, MTS: now.Add(-30 * time.Minute)},
		{Rate: 0.002, Amount: 1000, MTS: now},
	}

	sv := m.Compute(&domain.RawMarketData{
		RecentTrades: trades,
		Timestamp:    now,
	})

	// The old trade is beyond slowVWAPWindow (20min) and should be ignored.
	// Only the recent trade contributes, so VWAP should reflect 0.002.
	// With a single trade, fast and slow VWAP are the same → signal ≈ 0.
	if math.Abs(sv.Value) > 0.1 {
		t.Errorf("expected near-zero signal with single valid trade, got %f", sv.Value)
	}
	if sv.Confidence != 0.05 { // 1 trade / 20.0 = 0.05
		t.Errorf("expected confidence 0.05 for 1 valid trade, got %f", sv.Confidence)
	}
}
