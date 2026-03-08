package marketfeed

import (
	"testing"
	"time"

	"go.uber.org/zap"

	"github.com/will/bfx-funding-bot/backend/internal/bitfinex"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func testLogger() *zap.Logger {
	l, _ := zap.NewDevelopment()
	return l
}

// mockSignalSource implements SignalSource for testing.
type mockSignalSource struct {
	name  string
	value float64
}

func (m *mockSignalSource) Name() string { return m.name }
func (m *mockSignalSource) Compute(_ *domain.RawMarketData) domain.SignalValue {
	return domain.SignalValue{
		Type:       domain.SignalType(m.name),
		Value:      m.value,
		Confidence: 0.9,
		Timestamp:  time.Now(),
	}
}

func TestService_HandleTicker(t *testing.T) {
	svc := newTestService()

	svc.handleTicker(bitfinex.FundingTicker{
		Symbol:          "fUSD",
		FRR:             0.00025,
		Bid:             0.00024,
		Ask:             0.00026,
		DailyChangePerc: -0.05,
		Volume:          10000000,
	})

	st := svc.states["fUSD"]
	st.mu.RLock()
	defer st.mu.RUnlock()

	if st.ticker == nil {
		t.Fatal("ticker not set")
	}
	if st.ticker.FRR != 0.00025 {
		t.Errorf("FRR: got %f, want 0.00025", st.ticker.FRR)
	}
}

func TestService_HandleBookSnapshotAndUpdate(t *testing.T) {
	svc := newTestService()

	// Snapshot
	svc.handleBookSnapshot("fUSD", []bitfinex.BookEntry{
		{Symbol: "fUSD", Rate: 0.00025, Period: 2, Count: 5, Amount: 50000},
		{Symbol: "fUSD", Rate: 0.00024, Period: 2, Count: 3, Amount: -30000},
	})

	st := svc.states["fUSD"]
	st.mu.RLock()
	if len(st.book) != 2 {
		t.Fatalf("book entries: got %d, want 2", len(st.book))
	}
	st.mu.RUnlock()

	// Update: add new entry
	svc.handleBookUpdate("fUSD", bitfinex.BookEntry{
		Rate: 0.00026, Period: 7, Count: 2, Amount: 20000,
	})

	st.mu.RLock()
	if len(st.book) != 3 {
		t.Fatalf("book entries after add: got %d, want 3", len(st.book))
	}
	st.mu.RUnlock()

	// Update: delete entry (COUNT=0)
	svc.handleBookUpdate("fUSD", bitfinex.BookEntry{
		Rate: 0.00025, Period: 2, Count: 0, Amount: 1,
	})

	st.mu.RLock()
	if len(st.book) != 2 {
		t.Fatalf("book entries after delete: got %d, want 2", len(st.book))
	}
	st.mu.RUnlock()
}

func TestService_HandleTrades(t *testing.T) {
	svc := newTestService()

	svc.handleTradeSnapshot("fUSD", []bitfinex.FundingTrade{
		{Symbol: "fUSD", ID: 1, Amount: 5000, Rate: 0.00025, Period: 2, MTS: time.Now()},
		{Symbol: "fUSD", ID: 2, Amount: -3000, Rate: 0.00024, Period: 7, MTS: time.Now()},
	})

	st := svc.states["fUSD"]
	st.mu.RLock()
	if len(st.recentTrades) != 2 {
		t.Fatalf("trades: got %d, want 2", len(st.recentTrades))
	}
	st.mu.RUnlock()

	svc.handleTradeExecuted("fUSD", bitfinex.FundingTrade{
		Symbol: "fUSD", ID: 3, Amount: 1000, Rate: 0.00026, Period: 30, MTS: time.Now(),
	})

	st.mu.RLock()
	if len(st.recentTrades) != 3 {
		t.Fatalf("trades after add: got %d, want 3", len(st.recentTrades))
	}
	st.mu.RUnlock()
}

func TestService_BuildSnapshot(t *testing.T) {
	svc := newTestService()

	// No ticker → nil snapshot
	snap := svc.buildSnapshot("fUSD", time.Now())
	if snap != nil {
		t.Error("expected nil snapshot with no ticker")
	}

	// Set ticker
	svc.handleTicker(bitfinex.FundingTicker{
		Symbol: "fUSD", FRR: 0.00025, Bid: 0.00024, Ask: 0.00026,
		DailyChangePerc: -0.05,
	})
	svc.handleBookSnapshot("fUSD", []bitfinex.BookEntry{
		{Rate: 0.00026, Period: 2, Count: 5, Amount: 50000},
		{Rate: 0.00024, Period: 2, Count: 3, Amount: -30000},
	})

	snap = svc.buildSnapshot("fUSD", time.Now())
	if snap == nil {
		t.Fatal("expected non-nil snapshot")
	}
	if snap.Symbol != "fUSD" {
		t.Errorf("Symbol: got %s, want fUSD", snap.Symbol)
	}
	if snap.FlashFreeze {
		t.Error("expected no flash freeze")
	}
	if len(snap.Signals) != 1 {
		t.Errorf("Signals: got %d, want 1 (from mock)", len(snap.Signals))
	}
	if snap.OrderBook.EntryCount != 2 {
		t.Errorf("EntryCount: got %d, want 2", snap.OrderBook.EntryCount)
	}
}

func TestService_BuildSnapshot_FlashCrash(t *testing.T) {
	svc := newTestService()

	svc.handleTicker(bitfinex.FundingTicker{
		Symbol: "fUSD", FRR: 0.00025, DailyChangePerc: -0.40, // crash
	})

	snap := svc.buildSnapshot("fUSD", time.Now())
	if snap == nil {
		t.Fatal("expected non-nil snapshot")
	}
	if !snap.FlashFreeze {
		t.Error("expected flash freeze")
	}
}

func TestService_TradePruning(t *testing.T) {
	svc := newTestService()

	now := time.Now()
	svc.handleTicker(bitfinex.FundingTicker{Symbol: "fUSD", FRR: 0.00025})

	// Add old trade (6 min ago) and new trade (1 min ago)
	svc.handleTradeSnapshot("fUSD", []bitfinex.FundingTrade{
		{Symbol: "fUSD", ID: 1, Amount: 5000, Rate: 0.00025, MTS: now.Add(-6 * time.Minute)},
		{Symbol: "fUSD", ID: 2, Amount: 3000, Rate: 0.00025, MTS: now.Add(-1 * time.Minute)},
	})

	svc.buildSnapshot("fUSD", now)

	st := svc.states["fUSD"]
	st.mu.RLock()
	if len(st.recentTrades) != 1 {
		t.Errorf("after prune: got %d trades, want 1", len(st.recentTrades))
	}
	if st.recentTrades[0].ID != 2 {
		t.Errorf("remaining trade ID: got %d, want 2", st.recentTrades[0].ID)
	}
	st.mu.RUnlock()
}

func newTestService() *Service {
	return NewService(
		testLogger(),
		Config{
			Symbols:          []string{"fUSD"},
			SnapshotInterval: 1 * time.Second,
		},
		nil, // no cache in unit tests
		[]SignalSource{&mockSignalSource{name: "test_signal", value: 0.5}},
		nil,
	)
}
