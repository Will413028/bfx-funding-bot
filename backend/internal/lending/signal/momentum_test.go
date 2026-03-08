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

	// Feed trades at a constant rate across the slow window
	for i := 0; i < 20; i++ {
		ts := now.Add(-20*time.Minute + time.Duration(i)*time.Minute)
		m.Compute(&domain.RawMarketData{
			RecentTrades: []domain.FundingTradeRecord{
				{Rate: 0.001, Amount: 1000, MTS: ts},
			},
			Timestamp: ts,
		})
	}

	sv := m.Compute(&domain.RawMarketData{
		RecentTrades: []domain.FundingTradeRecord{
			{Rate: 0.001, Amount: 1000, MTS: now},
		},
		Timestamp: now,
	})
	// Fast VWAP ≈ Slow VWAP → signal ≈ 0
	if math.Abs(sv.Value) > 0.1 {
		t.Errorf("expected near-zero signal for flat rate, got %f", sv.Value)
	}
}

func TestMomentum_RisingRate(t *testing.T) {
	m := NewMomentum()
	now := time.Now()

	// Old trades at low rate
	for i := 0; i < 10; i++ {
		ts := now.Add(-15*time.Minute + time.Duration(i)*time.Minute)
		m.Compute(&domain.RawMarketData{
			RecentTrades: []domain.FundingTradeRecord{
				{Rate: 0.0005, Amount: 1000, MTS: ts},
			},
			Timestamp: ts,
		})
	}
	// Recent trades at high rate
	for i := 0; i < 5; i++ {
		ts := now.Add(-4*time.Minute + time.Duration(i)*time.Minute)
		m.Compute(&domain.RawMarketData{
			RecentTrades: []domain.FundingTradeRecord{
				{Rate: 0.002, Amount: 1000, MTS: ts},
			},
			Timestamp: ts,
		})
	}

	sv := m.Compute(&domain.RawMarketData{
		RecentTrades: []domain.FundingTradeRecord{
			{Rate: 0.002, Amount: 1000, MTS: now},
		},
		Timestamp: now,
	})
	// Fast VWAP > Slow VWAP → positive signal
	if sv.Value <= 0 {
		t.Errorf("expected positive signal for rising rate, got %f", sv.Value)
	}
}

func TestMomentum_TradesPruned(t *testing.T) {
	m := NewMomentum()
	now := time.Now()

	// Old trade beyond slow window
	m.Compute(&domain.RawMarketData{
		RecentTrades: []domain.FundingTradeRecord{
			{Rate: 0.001, Amount: 1000, MTS: now.Add(-30 * time.Minute)},
		},
		Timestamp: now.Add(-30 * time.Minute),
	})

	// New compute should prune old trades
	m.Compute(&domain.RawMarketData{
		RecentTrades: []domain.FundingTradeRecord{
			{Rate: 0.001, Amount: 1000, MTS: now},
		},
		Timestamp: now,
	})

	// Only 1 trade should remain (the old one pruned)
	if len(m.trades) != 1 {
		t.Errorf("expected 1 trade after pruning, got %d", len(m.trades))
	}
}
