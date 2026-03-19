package redis

import (
	"context"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	goredis "github.com/redis/go-redis/v9"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func newTestRedis(t *testing.T) (*goredis.Client, *miniredis.Miniredis) {
	t.Helper()
	mr := miniredis.RunT(t)
	client := goredis.NewClient(&goredis.Options{Addr: mr.Addr()})
	return client, mr
}

func sampleSnapshot() *domain.MarketSnapshot {
	return &domain.MarketSnapshot{
		Symbol: "fUSD",
		MDC: domain.MDCResult{
			Score:          0.65,
			DemandPressure: 0.8,
			SupplyPressure: 0.3,
			Timestamp:      time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC),
		},
		Regime:       domain.RegimeContango,
		RegimeParams: domain.RegimeParams{Volatility: 0.02, TrendStrength: 0.5, DemandSupplyRatio: 1.3, Duration: 2 * time.Hour},
		Signals: []domain.SignalValue{
			{Type: domain.SignalMDC, Value: 0.65, Confidence: 0.9, Timestamp: time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)},
		},
		OrderBook: domain.OrderBookSummary{
			BestBid: 0.00024, BestAsk: 0.00026, MidRate: 0.00025, Spread: 0.00002,
			BidDepth: 5000000, AskDepth: 3000000, EntryCount: 50,
		},
		WallPositions: []domain.WallPosition{
			{Rate: 0.0003, Amount: 1000000, Side: "offer", EntryCount: 3},
		},
		HiddenRatio: 0.15,
		FlashFreeze: false,
		Timestamp:   time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC),
	}
}

func TestSnapshotCache_SetAndGet(t *testing.T) {
	client, _ := newTestRedis(t)
	repo := NewSnapshotCacheRepo(client)
	ctx := context.Background()

	snap := sampleSnapshot()
	if err := repo.Set(ctx, "fUSD", snap, 60*time.Second); err != nil {
		t.Fatalf("Set failed: %v", err)
	}

	got, err := repo.Get(ctx, "fUSD")
	if err != nil {
		t.Fatalf("Get failed: %v", err)
	}
	if got == nil {
		t.Fatal("expected snapshot, got nil")
	}
	if got.Symbol != "fUSD" {
		t.Errorf("expected fUSD, got %s", got.Symbol)
	}
	if got.MDC.Score != 0.65 {
		t.Errorf("expected MDC score 0.65, got %f", got.MDC.Score)
	}
	if got.Regime != domain.RegimeContango {
		t.Errorf("expected contango, got %s", got.Regime)
	}
	if len(got.Signals) != 1 {
		t.Errorf("expected 1 signal, got %d", len(got.Signals))
	}
	if got.OrderBook.BestAsk != 0.00026 {
		t.Errorf("expected BestAsk 0.00026, got %f", got.OrderBook.BestAsk)
	}
}

func TestSnapshotCache_GetMiss(t *testing.T) {
	client, _ := newTestRedis(t)
	repo := NewSnapshotCacheRepo(client)
	ctx := context.Background()

	got, err := repo.Get(ctx, "fBTC")
	if err != nil {
		t.Fatalf("Get failed: %v", err)
	}
	if got != nil {
		t.Errorf("expected nil for cache miss, got %+v", got)
	}
}

func TestSnapshotCache_TTLExpiry(t *testing.T) {
	client, mr := newTestRedis(t)
	repo := NewSnapshotCacheRepo(client)
	ctx := context.Background()

	snap := sampleSnapshot()
	_ = repo.Set(ctx, "fUSD", snap, 1*time.Second)

	// Fast-forward time in miniredis
	mr.FastForward(2 * time.Second)

	got, err := repo.Get(ctx, "fUSD")
	if err != nil {
		t.Fatalf("Get failed: %v", err)
	}
	if got != nil {
		t.Error("expected nil after TTL expiry")
	}
}

func TestSnapshotCache_Overwrite(t *testing.T) {
	client, _ := newTestRedis(t)
	repo := NewSnapshotCacheRepo(client)
	ctx := context.Background()

	snap1 := sampleSnapshot()
	snap1.HiddenRatio = 0.1
	_ = repo.Set(ctx, "fUSD", snap1, 60*time.Second)

	snap2 := sampleSnapshot()
	snap2.HiddenRatio = 0.9
	_ = repo.Set(ctx, "fUSD", snap2, 60*time.Second)

	got, _ := repo.Get(ctx, "fUSD")
	if got.HiddenRatio != 0.9 {
		t.Errorf("expected overwritten HiddenRatio 0.9, got %f", got.HiddenRatio)
	}
}

func TestSnapshotCache_Delete(t *testing.T) {
	client, _ := newTestRedis(t)
	repo := NewSnapshotCacheRepo(client)
	ctx := context.Background()

	snap := sampleSnapshot()
	_ = repo.Set(ctx, "fUSD", snap, 60*time.Second)

	if err := repo.Delete(ctx, "fUSD"); err != nil {
		t.Fatalf("Delete failed: %v", err)
	}

	got, _ := repo.Get(ctx, "fUSD")
	if got != nil {
		t.Error("expected nil after delete")
	}
}

func TestSnapshotCache_DeleteNonExisting(t *testing.T) {
	client, _ := newTestRedis(t)
	repo := NewSnapshotCacheRepo(client)
	ctx := context.Background()

	// Should not error
	if err := repo.Delete(ctx, "nonexistent"); err != nil {
		t.Fatalf("Delete non-existing should not error: %v", err)
	}
}
