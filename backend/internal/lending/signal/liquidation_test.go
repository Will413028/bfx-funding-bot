package signal

import (
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestLiquidationCascade_NoLargeTrades(t *testing.T) {
	lc := NewLiquidationCascade()
	now := time.Now()
	data := &domain.RawMarketData{
		RecentTrades: []domain.FundingTradeRecord{
			{Rate: 0.001, Amount: 1000, MTS: now},
		},
		Timestamp: now,
	}
	sv := lc.Compute(data)
	if sv.Value != 0 {
		t.Errorf("expected zero for small trades, got %f", sv.Value)
	}
}

func TestLiquidationCascade_Trigger(t *testing.T) {
	lc := NewLiquidationCascade()
	now := time.Now()

	// Generate enough large trades to exceed volume threshold (5M)
	trades := make([]domain.FundingTradeRecord, 0)
	for i := 0; i < 10; i++ {
		trades = append(trades, domain.FundingTradeRecord{
			Rate:   0.001,
			Amount: 600000, // 600k each → 6M total > 5M threshold
			MTS:    now.Add(time.Duration(i) * time.Second),
		})
	}

	data := &domain.RawMarketData{RecentTrades: trades, Timestamp: now.Add(10 * time.Second)}
	sv := lc.Compute(data)

	if sv.Value != 1.0 {
		t.Errorf("expected 1.0 after trigger, got %f", sv.Value)
	}
	if sv.Confidence != 1.0 {
		t.Errorf("expected confidence 1.0, got %f", sv.Confidence)
	}
}

func TestLiquidationCascade_Regression(t *testing.T) {
	lc := NewLiquidationCascade()
	now := time.Now()

	// Trigger first
	trades := make([]domain.FundingTradeRecord, 0)
	for i := 0; i < 10; i++ {
		trades = append(trades, domain.FundingTradeRecord{
			Rate: 0.001, Amount: 600000, MTS: now,
		})
	}
	lc.Compute(&domain.RawMarketData{RecentTrades: trades, Timestamp: now})

	// After 1 regression interval (5min) with no new large trades
	sv := lc.Compute(&domain.RawMarketData{
		RecentTrades: []domain.FundingTradeRecord{},
		Timestamp:    now.Add(6 * time.Minute),
	})
	if sv.Value != 0.7 {
		t.Errorf("expected 0.7 after 1 interval, got %f", sv.Value)
	}

	// After 2 intervals (10min)
	sv = lc.Compute(&domain.RawMarketData{
		RecentTrades: []domain.FundingTradeRecord{},
		Timestamp:    now.Add(11 * time.Minute),
	})
	if sv.Value != 0.3 {
		t.Errorf("expected 0.3 after 2 intervals, got %f", sv.Value)
	}

	// After 3 intervals (15min) → fully cleared
	sv = lc.Compute(&domain.RawMarketData{
		RecentTrades: []domain.FundingTradeRecord{},
		Timestamp:    now.Add(16 * time.Minute),
	})
	if sv.Value != 0 {
		t.Errorf("expected 0 after 3 intervals, got %f", sv.Value)
	}
}

func TestLiquidationCascade_ReTrigger(t *testing.T) {
	lc := NewLiquidationCascade()
	now := time.Now()

	// Trigger
	trades := make([]domain.FundingTradeRecord, 10)
	for i := range trades {
		trades[i] = domain.FundingTradeRecord{Rate: 0.001, Amount: 600000, MTS: now}
	}
	lc.Compute(&domain.RawMarketData{RecentTrades: trades, Timestamp: now})

	// Regress to 0.7
	lc.Compute(&domain.RawMarketData{
		RecentTrades: []domain.FundingTradeRecord{},
		Timestamp:    now.Add(6 * time.Minute),
	})

	// Re-trigger during regression
	newTrades := make([]domain.FundingTradeRecord, 10)
	retriggerTime := now.Add(6*time.Minute + 30*time.Second)
	for i := range newTrades {
		newTrades[i] = domain.FundingTradeRecord{Rate: 0.001, Amount: 600000, MTS: retriggerTime}
	}
	sv := lc.Compute(&domain.RawMarketData{
		RecentTrades: newTrades,
		Timestamp:    retriggerTime,
	})
	if sv.Value != 1.0 {
		t.Errorf("expected 1.0 after re-trigger, got %f", sv.Value)
	}
}
