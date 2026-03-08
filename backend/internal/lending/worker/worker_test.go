package worker

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// --- Mock dependencies ---

type mockStrategy struct {
	applyFn func(ctx *domain.DecisionContext) *domain.DecisionResult
	calls   int32
}

func (m *mockStrategy) Apply(ctx *domain.DecisionContext) *domain.DecisionResult {
	atomic.AddInt32(&m.calls, 1)
	if m.applyFn != nil {
		return m.applyFn(ctx)
	}
	return &domain.DecisionResult{
		Offers: []domain.OfferDecision{{Amount: 1000, Rate: 0.0003, Period: 5}},
		Reason: "test",
	}
}

type mockDataFetcher struct {
	fetchFn func(ctx context.Context, userID string) (*UserData, error)
	calls   int32
}

func (m *mockDataFetcher) FetchUserData(ctx context.Context, userID string) (*UserData, error) {
	atomic.AddInt32(&m.calls, 1)
	if m.fetchFn != nil {
		return m.fetchFn(ctx, userID)
	}
	return &UserData{
		Available:     5000,
		ActiveOffers:  nil,
		ActiveCredits: nil,
	}, nil
}

type mockOfferExecutor struct {
	executeFn func(ctx context.Context, userID string, decision *domain.DecisionResult) (*ExecutionSummary, error)
	calls     int32
}

func (m *mockOfferExecutor) ExecuteDecision(ctx context.Context, userID string, decision *domain.DecisionResult) (*ExecutionSummary, error) {
	atomic.AddInt32(&m.calls, 1)
	if m.executeFn != nil {
		return m.executeFn(ctx, userID, decision)
	}
	return &ExecutionSummary{PlaceOK: 1}, nil
}

// --- Helpers ---

func workerConfig() domain.StrategyConfig {
	return domain.StrategyConfig{
		Currency: "fUSD",
		Amount:   domain.AmountConfig{Min: 50, Max: 10000},
		Rate:     domain.RateConfig{Min: 0.0002, Max: 0.005},
		Period:   domain.PeriodConfig{Min: 2, Max: 30},
	}
}

func testSnapshot() *domain.MarketSnapshot {
	return &domain.MarketSnapshot{
		Symbol:    "fUSD",
		FRR:       0.0003,
		Timestamp: time.Now(),
	}
}

func newTestWorker(snapshotCh chan *domain.MarketSnapshot, strategy *mockStrategy, fetcher *mockDataFetcher, executor *mockOfferExecutor) *LendingWorker {
	return NewLendingWorker("user-1", workerConfig(), Deps{
		Strategy:   strategy,
		Fetcher:    fetcher,
		Executor:   executor,
		SnapshotCh: snapshotCh,
	})
}

// --- Tests ---

func TestWorker_InterfaceSatisfaction(t *testing.T) {
	var _ Worker = (*LendingWorker)(nil)
}

func TestWorker_SnapshotTriggersTick(t *testing.T) {
	snapshotCh := make(chan *domain.MarketSnapshot, 1)
	strategy := &mockStrategy{}
	fetcher := &mockDataFetcher{}
	executor := &mockOfferExecutor{}
	w := newTestWorker(snapshotCh, strategy, fetcher, executor)

	ctx, cancel := context.WithCancel(context.Background())

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		w.Run(ctx)
	}()

	// Wait for worker to start
	time.Sleep(10 * time.Millisecond)

	// Send snapshot
	snapshotCh <- testSnapshot()

	// Wait for tick to process
	time.Sleep(50 * time.Millisecond)

	if atomic.LoadInt32(&fetcher.calls) != 1 {
		t.Errorf("fetcher calls: got %d, want 1", fetcher.calls)
	}
	if atomic.LoadInt32(&strategy.calls) != 1 {
		t.Errorf("strategy calls: got %d, want 1", strategy.calls)
	}
	if atomic.LoadInt32(&executor.calls) != 1 {
		t.Errorf("executor calls: got %d, want 1", executor.calls)
	}

	cancel()
	wg.Wait()
}

func TestWorker_NoSnapshotNoTick(t *testing.T) {
	snapshotCh := make(chan *domain.MarketSnapshot, 1)
	strategy := &mockStrategy{}
	fetcher := &mockDataFetcher{}
	executor := &mockOfferExecutor{}
	w := newTestWorker(snapshotCh, strategy, fetcher, executor)

	ctx, cancel := context.WithCancel(context.Background())

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		w.Run(ctx)
	}()

	// Wait without sending snapshot
	time.Sleep(50 * time.Millisecond)

	if atomic.LoadInt32(&fetcher.calls) != 0 {
		t.Errorf("fetcher calls: got %d, want 0", fetcher.calls)
	}

	cancel()
	wg.Wait()
}

func TestWorker_DataFetcherError(t *testing.T) {
	snapshotCh := make(chan *domain.MarketSnapshot, 1)
	strategy := &mockStrategy{}
	fetcher := &mockDataFetcher{
		fetchFn: func(ctx context.Context, userID string) (*UserData, error) {
			return nil, errors.New("fetch error")
		},
	}
	executor := &mockOfferExecutor{}
	w := newTestWorker(snapshotCh, strategy, fetcher, executor)

	ctx, cancel := context.WithCancel(context.Background())

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		w.Run(ctx)
	}()

	time.Sleep(10 * time.Millisecond)
	snapshotCh <- testSnapshot()
	time.Sleep(50 * time.Millisecond)

	// Strategy and executor should NOT be called
	if atomic.LoadInt32(&strategy.calls) != 0 {
		t.Errorf("strategy calls: got %d, want 0 (fetch failed)", strategy.calls)
	}
	if atomic.LoadInt32(&executor.calls) != 0 {
		t.Errorf("executor calls: got %d, want 0 (fetch failed)", executor.calls)
	}

	cancel()
	wg.Wait()
}

func TestWorker_ContextCancelled(t *testing.T) {
	snapshotCh := make(chan *domain.MarketSnapshot, 1)
	w := newTestWorker(snapshotCh, &mockStrategy{}, &mockDataFetcher{}, &mockOfferExecutor{})

	ctx, cancel := context.WithCancel(context.Background())

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		w.Run(ctx)
	}()

	time.Sleep(10 * time.Millisecond)
	cancel()
	wg.Wait()

	if w.State() != StateStopped {
		t.Errorf("state: got %s, want stopped", w.State())
	}
}

func TestWorker_StopMethod(t *testing.T) {
	snapshotCh := make(chan *domain.MarketSnapshot, 1)
	w := newTestWorker(snapshotCh, &mockStrategy{}, &mockDataFetcher{}, &mockOfferExecutor{})

	ctx := context.Background()

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		w.Run(ctx)
	}()

	time.Sleep(10 * time.Millisecond)
	w.Stop()

	// Should exit quickly
	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()

	select {
	case <-done:
		// ok
	case <-time.After(1 * time.Second):
		t.Fatal("worker did not stop within 1 second")
	}

	if w.State() != StateStopped {
		t.Errorf("state: got %s, want stopped", w.State())
	}

	// Stop again should be safe
	w.Stop()
}

func TestWorker_ConfigReload(t *testing.T) {
	snapshotCh := make(chan *domain.MarketSnapshot, 2)
	var receivedConfig atomic.Value
	strategy := &mockStrategy{
		applyFn: func(ctx *domain.DecisionContext) *domain.DecisionResult {
			receivedConfig.Store(ctx.Config.Rate.Min)
			return &domain.DecisionResult{}
		},
	}
	fetcher := &mockDataFetcher{}
	executor := &mockOfferExecutor{}
	w := newTestWorker(snapshotCh, strategy, fetcher, executor)

	ctx, cancel := context.WithCancel(context.Background())

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		w.Run(ctx)
	}()

	time.Sleep(10 * time.Millisecond)

	// First tick with original config
	snapshotCh <- testSnapshot()
	time.Sleep(50 * time.Millisecond)

	val := receivedConfig.Load()
	if val == nil || val.(float64) != 0.0002 {
		t.Errorf("first tick rate min: got %v, want 0.0002", val)
	}

	// Reload config
	newCfg := workerConfig()
	newCfg.Rate.Min = 0.0005
	w.ReloadConfig(newCfg)

	// Second tick with new config
	snapshotCh <- testSnapshot()
	time.Sleep(50 * time.Millisecond)

	val = receivedConfig.Load()
	if val == nil || val.(float64) != 0.0005 {
		t.Errorf("second tick rate min: got %v, want 0.0005", val)
	}

	cancel()
	wg.Wait()
}

func TestWorker_PanicRecovery(t *testing.T) {
	snapshotCh := make(chan *domain.MarketSnapshot, 2)
	callCount := int32(0)
	strategy := &mockStrategy{
		applyFn: func(ctx *domain.DecisionContext) *domain.DecisionResult {
			n := atomic.AddInt32(&callCount, 1)
			if n == 1 {
				panic("test panic")
			}
			return &domain.DecisionResult{
				Offers: []domain.OfferDecision{{Amount: 1000, Rate: 0.0003, Period: 5}},
			}
		},
	}
	fetcher := &mockDataFetcher{}
	executor := &mockOfferExecutor{}
	w := newTestWorker(snapshotCh, strategy, fetcher, executor)

	var panicRecovered int32
	w.onError = func(userID string, err interface{}) {
		atomic.AddInt32(&panicRecovered, 1)
	}

	ctx, cancel := context.WithCancel(context.Background())

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		w.Run(ctx)
	}()

	time.Sleep(10 * time.Millisecond)

	// First snapshot: will panic
	snapshotCh <- testSnapshot()
	time.Sleep(50 * time.Millisecond)

	if atomic.LoadInt32(&panicRecovered) != 1 {
		t.Errorf("panic recovered: got %d, want 1", panicRecovered)
	}

	// Second snapshot: should work normally
	snapshotCh <- testSnapshot()
	time.Sleep(50 * time.Millisecond)

	if atomic.LoadInt32(&executor.calls) != 1 {
		t.Errorf("executor calls: got %d, want 1 (second tick should succeed)", executor.calls)
	}

	cancel()
	wg.Wait()

	if w.State() != StateStopped {
		t.Errorf("state: got %s, want stopped", w.State())
	}
}

func TestWorker_StateTransitions(t *testing.T) {
	snapshotCh := make(chan *domain.MarketSnapshot, 1)
	w := newTestWorker(snapshotCh, &mockStrategy{}, &mockDataFetcher{}, &mockOfferExecutor{})

	if w.State() != StateStarting {
		t.Errorf("initial state: got %s, want starting", w.State())
	}

	ctx, cancel := context.WithCancel(context.Background())

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		w.Run(ctx)
	}()

	time.Sleep(10 * time.Millisecond)

	if w.State() != StateRunning {
		t.Errorf("running state: got %s, want running", w.State())
	}

	w.Stop()
	time.Sleep(10 * time.Millisecond)

	cancel()
	wg.Wait()

	if w.State() != StateStopped {
		t.Errorf("final state: got %s, want stopped", w.State())
	}
}

func TestWorker_UserID(t *testing.T) {
	snapshotCh := make(chan *domain.MarketSnapshot)
	w := newTestWorker(snapshotCh, &mockStrategy{}, &mockDataFetcher{}, &mockOfferExecutor{})

	if w.UserID() != "user-1" {
		t.Errorf("UserID: got %s, want user-1", w.UserID())
	}
}

func TestWorker_ChannelClosed(t *testing.T) {
	snapshotCh := make(chan *domain.MarketSnapshot, 1)
	w := newTestWorker(snapshotCh, &mockStrategy{}, &mockDataFetcher{}, &mockOfferExecutor{})

	ctx := context.Background()

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		w.Run(ctx)
	}()

	time.Sleep(10 * time.Millisecond)
	close(snapshotCh)

	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()

	select {
	case <-done:
		// ok
	case <-time.After(1 * time.Second):
		t.Fatal("worker did not stop after channel close")
	}
}

func TestWorker_EmptyDecision(t *testing.T) {
	snapshotCh := make(chan *domain.MarketSnapshot, 1)
	strategy := &mockStrategy{
		applyFn: func(ctx *domain.DecisionContext) *domain.DecisionResult {
			return &domain.DecisionResult{}
		},
	}
	executor := &mockOfferExecutor{}
	w := newTestWorker(snapshotCh, strategy, &mockDataFetcher{}, executor)

	ctx, cancel := context.WithCancel(context.Background())

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		w.Run(ctx)
	}()

	time.Sleep(10 * time.Millisecond)
	snapshotCh <- testSnapshot()
	time.Sleep(50 * time.Millisecond)

	// Executor should still be called with empty decision
	if atomic.LoadInt32(&executor.calls) != 1 {
		t.Errorf("executor calls: got %d, want 1", executor.calls)
	}

	cancel()
	wg.Wait()
}
