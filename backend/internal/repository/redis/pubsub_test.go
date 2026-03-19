package redis

import (
	"context"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	goredis "github.com/redis/go-redis/v9"
	"go.uber.org/zap"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func newTestPubSub(t *testing.T) (*goredis.Client, *zap.Logger) {
	t.Helper()
	mr := miniredis.RunT(t)
	client := goredis.NewClient(&goredis.Options{Addr: mr.Addr()})
	log, _ := zap.NewDevelopment()
	return client, log
}

func TestSnapshotPubSub_PublishAndSubscribe(t *testing.T) {
	client, log := newTestPubSub(t)
	ctx := context.Background()

	pub := NewSnapshotPubSubRepo(client, log)
	sub := NewSnapshotPubSubRepo(client, log)

	ch, err := sub.Subscribe(ctx)
	if err != nil {
		t.Fatalf("Subscribe failed: %v", err)
	}
	defer sub.Close()

	// Give subscriber time to set up
	time.Sleep(50 * time.Millisecond)

	snap := sampleSnapshot()
	if err := pub.Publish(ctx, snap); err != nil {
		t.Fatalf("Publish failed: %v", err)
	}

	select {
	case received := <-ch:
		if received == nil {
			t.Fatal("received nil snapshot")
		}
		if received.Symbol != "fUSD" {
			t.Errorf("expected fUSD, got %s", received.Symbol)
		}
		if received.MDC.Score != 0.65 {
			t.Errorf("expected MDC score 0.65, got %f", received.MDC.Score)
		}
		if received.Regime != domain.RegimeContango {
			t.Errorf("expected contango, got %s", received.Regime)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timeout waiting for published snapshot")
	}
}

func TestSnapshotPubSub_Close(t *testing.T) {
	client, log := newTestPubSub(t)
	ctx := context.Background()

	sub := NewSnapshotPubSubRepo(client, log)
	ch, err := sub.Subscribe(ctx)
	if err != nil {
		t.Fatalf("Subscribe failed: %v", err)
	}

	if err := sub.Close(); err != nil {
		t.Fatalf("Close failed: %v", err)
	}

	// Channel should be closed
	select {
	case _, ok := <-ch:
		if ok {
			t.Error("expected channel to be closed")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timeout waiting for channel close")
	}
}

func TestSnapshotPubSub_MultipleMessages(t *testing.T) {
	client, log := newTestPubSub(t)
	ctx := context.Background()

	pub := NewSnapshotPubSubRepo(client, log)
	sub := NewSnapshotPubSubRepo(client, log)

	ch, err := sub.Subscribe(ctx)
	if err != nil {
		t.Fatalf("Subscribe failed: %v", err)
	}
	defer sub.Close()

	time.Sleep(50 * time.Millisecond)

	// Publish 3 snapshots with different symbols
	symbols := []string{"fUSD", "fBTC", "fETH"}
	for _, sym := range symbols {
		snap := sampleSnapshot()
		snap.Symbol = sym
		_ = pub.Publish(ctx, snap)
	}

	received := make([]string, 0, 3)
	for i := 0; i < 3; i++ {
		select {
		case s := <-ch:
			received = append(received, s.Symbol)
		case <-time.After(2 * time.Second):
			t.Fatalf("timeout waiting for snapshot %d", i+1)
		}
	}

	if len(received) != 3 {
		t.Errorf("expected 3 snapshots, got %d", len(received))
	}
}
