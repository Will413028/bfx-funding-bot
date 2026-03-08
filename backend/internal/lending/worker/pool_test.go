package worker

import (
	"context"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// --- Mock Worker ---

type mockWorker struct {
	userID   string
	stopCh   chan struct{}
	reloaded []domain.StrategyConfig
	mu       sync.Mutex
	runErr   error
}

func newMockWorker(_ context.Context, userID string, _ domain.StrategyConfig) Worker {
	return &mockWorker{
		userID: userID,
		stopCh: make(chan struct{}),
	}
}

func (m *mockWorker) Run(ctx context.Context) error {
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-m.stopCh:
		return m.runErr
	}
}

func (m *mockWorker) Stop() {
	select {
	case <-m.stopCh:
		// already closed
	default:
		close(m.stopCh)
	}
}

func (m *mockWorker) ReloadConfig(config domain.StrategyConfig) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.reloaded = append(m.reloaded, config)
}

func (m *mockWorker) UserID() string {
	return m.userID
}

// --- Helpers ---

func testConfig() domain.StrategyConfig {
	return domain.StrategyConfig{
		Currency: "fUSD",
		Amount:   domain.AmountConfig{Min: 50, Max: 10000},
		Rate:     domain.RateConfig{Min: 0.0002, Max: 0.005},
		Period:   domain.PeriodConfig{Min: 2, Max: 30},
	}
}

// --- Tests ---

func TestPool_StartNew(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	pool := NewPool(ctx, newMockWorker)

	err := pool.Start("u1", testConfig())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Give goroutine time to start
	time.Sleep(10 * time.Millisecond)

	if pool.Count() != 1 {
		t.Errorf("Count: got %d, want 1", pool.Count())
	}
	if !pool.Has("u1") {
		t.Error("Has(u1): got false, want true")
	}
}

func TestPool_StartDuplicate(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	pool := NewPool(ctx, newMockWorker)

	_ = pool.Start("u1", testConfig())
	err := pool.Start("u1", testConfig())

	if err == nil {
		t.Fatal("expected error for duplicate start")
	}
}

func TestPool_Stop(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	pool := NewPool(ctx, newMockWorker)
	_ = pool.Start("u1", testConfig())

	time.Sleep(10 * time.Millisecond)

	err := pool.Stop("u1")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Give cleanup goroutine time
	time.Sleep(20 * time.Millisecond)

	if pool.Has("u1") {
		t.Error("Has(u1): got true, want false after Stop")
	}
	if pool.Count() != 0 {
		t.Errorf("Count: got %d, want 0", pool.Count())
	}
}

func TestPool_StopNonExistent(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	pool := NewPool(ctx, newMockWorker)

	err := pool.Stop("u1")
	if err == nil {
		t.Fatal("expected error for stopping non-existent worker")
	}
}

func TestPool_StopAll(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	pool := NewPool(ctx, newMockWorker)
	_ = pool.Start("u1", testConfig())
	_ = pool.Start("u2", testConfig())
	_ = pool.Start("u3", testConfig())

	time.Sleep(10 * time.Millisecond)
	if pool.Count() != 3 {
		t.Fatalf("Count before StopAll: got %d, want 3", pool.Count())
	}

	stopCtx, stopCancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer stopCancel()

	err := pool.StopAll(stopCtx)
	if err != nil {
		t.Fatalf("StopAll error: %v", err)
	}

	if pool.Count() != 0 {
		t.Errorf("Count after StopAll: got %d, want 0", pool.Count())
	}
}

func TestPool_Reload(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var createdWorker *mockWorker
	factory := func(ctx context.Context, userID string, config domain.StrategyConfig) Worker {
		w := &mockWorker{userID: userID, stopCh: make(chan struct{})}
		createdWorker = w
		return w
	}

	pool := NewPool(ctx, factory)
	_ = pool.Start("u1", testConfig())

	time.Sleep(10 * time.Millisecond)

	newCfg := testConfig()
	newCfg.Rate.Min = 0.0005

	err := pool.Reload("u1", newCfg)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	createdWorker.mu.Lock()
	defer createdWorker.mu.Unlock()
	if len(createdWorker.reloaded) != 1 {
		t.Fatalf("reloaded: got %d, want 1", len(createdWorker.reloaded))
	}
	if createdWorker.reloaded[0].Rate.Min != 0.0005 {
		t.Errorf("reloaded rate min: got %f, want 0.0005", createdWorker.reloaded[0].Rate.Min)
	}
}

func TestPool_ReloadNonExistent(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	pool := NewPool(ctx, newMockWorker)

	err := pool.Reload("u1", testConfig())
	if err == nil {
		t.Fatal("expected error for reloading non-existent worker")
	}
}

func TestPool_CountAndHas(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	pool := NewPool(ctx, newMockWorker)
	_ = pool.Start("u1", testConfig())
	_ = pool.Start("u2", testConfig())

	time.Sleep(10 * time.Millisecond)

	if pool.Count() != 2 {
		t.Errorf("Count: got %d, want 2", pool.Count())
	}
	if !pool.Has("u1") {
		t.Error("Has(u1): got false, want true")
	}
	if !pool.Has("u2") {
		t.Error("Has(u2): got false, want true")
	}
	if pool.Has("u3") {
		t.Error("Has(u3): got true, want false")
	}
}

func TestPool_AutoCleanupOnExit(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var createdWorker *mockWorker
	factory := func(ctx context.Context, userID string, config domain.StrategyConfig) Worker {
		w := &mockWorker{userID: userID, stopCh: make(chan struct{})}
		createdWorker = w
		return w
	}

	pool := NewPool(ctx, factory)
	_ = pool.Start("u1", testConfig())

	time.Sleep(10 * time.Millisecond)
	if pool.Count() != 1 {
		t.Fatalf("Count before exit: got %d, want 1", pool.Count())
	}

	// Simulate worker exiting on its own
	createdWorker.Stop()

	// Give cleanup goroutine time
	time.Sleep(50 * time.Millisecond)

	if pool.Count() != 0 {
		t.Errorf("Count after auto-cleanup: got %d, want 0", pool.Count())
	}
	if pool.Has("u1") {
		t.Error("Has(u1) after auto-cleanup: got true, want false")
	}
}

func TestPool_ConcurrentStartStop(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	pool := NewPool(ctx, newMockWorker)

	var ops int64
	var wg sync.WaitGroup

	// Concurrent starts
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			userID := fmt.Sprintf("u%d", id)
			if err := pool.Start(userID, testConfig()); err == nil {
				atomic.AddInt64(&ops, 1)
			}
		}(i)
	}
	wg.Wait()

	if atomic.LoadInt64(&ops) != 20 {
		t.Errorf("successful starts: got %d, want 20", atomic.LoadInt64(&ops))
	}

	time.Sleep(10 * time.Millisecond)

	if pool.Count() != 20 {
		t.Errorf("Count: got %d, want 20", pool.Count())
	}

	// Concurrent stops
	var stopOps int64
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			userID := fmt.Sprintf("u%d", id)
			if err := pool.Stop(userID); err == nil {
				atomic.AddInt64(&stopOps, 1)
			}
		}(i)
	}
	wg.Wait()

	time.Sleep(50 * time.Millisecond)

	if atomic.LoadInt64(&stopOps) != 20 {
		t.Errorf("successful stops: got %d, want 20", atomic.LoadInt64(&stopOps))
	}
	if pool.Count() != 0 {
		t.Errorf("Count after stops: got %d, want 0", pool.Count())
	}
}
