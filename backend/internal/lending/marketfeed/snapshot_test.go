package marketfeed

import (
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/lending/orderbook"
)

func TestComputeOrderBookSummary_Normal(t *testing.T) {
	entries := []domain.BookEntry{
		{Rate: 0.00024, Period: 2, Count: 3, Amount: -30000},  // bid
		{Rate: 0.00023, Period: 7, Count: 2, Amount: -20000},  // bid
		{Rate: 0.00026, Period: 2, Count: 5, Amount: 50000},   // offer
		{Rate: 0.00027, Period: 30, Count: 2, Amount: 40000},  // offer
	}

	s := orderbook.ComputeSummary(entries)

	if s.BestBid != 0.00024 {
		t.Errorf("BestBid: got %f, want 0.00024", s.BestBid)
	}
	if s.BestAsk != 0.00026 {
		t.Errorf("BestAsk: got %f, want 0.00026", s.BestAsk)
	}
	if s.BidDepth != 50000 {
		t.Errorf("BidDepth: got %f, want 50000", s.BidDepth)
	}
	if s.AskDepth != 90000 {
		t.Errorf("AskDepth: got %f, want 90000", s.AskDepth)
	}
	if s.EntryCount != 4 {
		t.Errorf("EntryCount: got %d, want 4", s.EntryCount)
	}
	expectedSpread := 0.00026 - 0.00024
	if s.Spread < expectedSpread-1e-10 || s.Spread > expectedSpread+1e-10 {
		t.Errorf("Spread: got %f, want %f", s.Spread, expectedSpread)
	}
}

func TestComputeOrderBookSummary_Empty(t *testing.T) {
	s := orderbook.ComputeSummary(nil)
	if s.EntryCount != 0 {
		t.Errorf("EntryCount: got %d, want 0", s.EntryCount)
	}
}

func TestDetectWalls(t *testing.T) {
	// Total offer = 100k + 1k + 1k = 102k; wall threshold 50% = 51k
	// Total bid = 1k + 95k = 96k; wall threshold 50% = 48k
	entries := []domain.BookEntry{
		{Rate: 0.00025, Period: 2, Count: 1, Amount: 100000},  // 100k offer — wall
		{Rate: 0.00026, Period: 2, Count: 3, Amount: 1000},    // 1k offer — not
		{Rate: 0.000265, Period: 2, Count: 2, Amount: 1000},   // 1k offer — not
		{Rate: 0.00024, Period: 2, Count: 2, Amount: -1000},   // 1k bid — not
		{Rate: 0.00023, Period: 2, Count: 1, Amount: -95000},  // 95k bid — wall
	}

	walls := orderbook.DetectWalls(entries, 0, &orderbook.WallOptions{Threshold: 0.50})

	if len(walls) != 2 {
		t.Fatalf("expected 2 walls, got %d", len(walls))
	}

	var foundOffer, foundBid bool
	for _, w := range walls {
		if w.Side == "offer" && w.Rate == 0.00025 && w.Amount == 100000 {
			foundOffer = true
		}
		if w.Side == "bid" && w.Rate == 0.00023 && w.Amount == 95000 {
			foundBid = true
		}
	}
	if !foundOffer {
		t.Error("expected offer wall at 0.00025")
	}
	if !foundBid {
		t.Error("expected bid wall at 0.00023")
	}
}

func TestDetectWalls_NoWalls(t *testing.T) {
	entries := []domain.BookEntry{
		{Rate: 0.00025, Period: 2, Count: 1, Amount: 1000},
		{Rate: 0.00026, Period: 2, Count: 1, Amount: 1000},
		{Rate: 0.00027, Period: 2, Count: 1, Amount: 1000},
	}

	walls := orderbook.DetectWalls(entries, 0, &orderbook.WallOptions{Threshold: 0.50})
	if len(walls) != 0 {
		t.Errorf("expected 0 walls, got %d", len(walls))
	}
}

func TestAssembleSnapshot(t *testing.T) {
	raw := &domain.RawMarketData{
		Symbol: "fUSD",
		Ticker: &domain.FundingTicker{FRR: 0.00025},
		Book: []domain.BookEntry{
			{Rate: 0.00026, Amount: 50000, Count: 5, Period: 2},
			{Rate: 0.00024, Amount: -30000, Count: 3, Period: 2},
		},
		Timestamp: time.Now(),
	}
	signals := []domain.SignalValue{
		{Type: domain.SignalMDC, Value: 0.5, Confidence: 0.8},
	}
	now := time.Now()

	snap := assembleSnapshot("fUSD", raw, signals, domain.RegimeContango, domain.RegimeParams{}, false, 0.3, 0.5, now)

	if snap.Symbol != "fUSD" {
		t.Errorf("Symbol: got %s, want fUSD", snap.Symbol)
	}
	if snap.Regime != domain.RegimeContango {
		t.Errorf("Regime: got %s, want contango", snap.Regime)
	}
	if len(snap.Signals) != 1 {
		t.Errorf("Signals: got %d, want 1", len(snap.Signals))
	}
	if snap.OrderBook.BestAsk != 0.00026 {
		t.Errorf("BestAsk: got %f, want 0.00026", snap.OrderBook.BestAsk)
	}
	if snap.FlashFreeze {
		t.Error("expected no flash freeze")
	}
	if snap.HiddenRatio != 0.3 {
		t.Errorf("HiddenRatio: got %f, want 0.3", snap.HiddenRatio)
	}
	if snap.CompetitorActivity != 0.5 {
		t.Errorf("CompetitorActivity: got %f, want 0.5", snap.CompetitorActivity)
	}
}
