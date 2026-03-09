package lending

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/lending/quota"
	"github.com/will/bfx-funding-bot/backend/internal/lending/worker"
)

// --- Mock WorkerDepsFactory ---

type mockStrategy struct{}

func (m *mockStrategy) Apply(ctx *domain.DecisionContext) *domain.DecisionResult {
	return &domain.DecisionResult{}
}

type mockDataFetcher struct{}

func (m *mockDataFetcher) FetchUserData(ctx context.Context, userID string) (*worker.UserData, error) {
	return &worker.UserData{Available: 5000}, nil
}

type mockOfferExecutor struct{}

func (m *mockOfferExecutor) ExecuteDecision(ctx context.Context, userID string, decision *domain.DecisionResult) (*worker.ExecutionSummary, error) {
	return &worker.ExecutionSummary{}, nil
}

type mockDepsFactory struct{}

func (m *mockDepsFactory) BuildWorkerDeps(userID string, currency string, snapshotCh <-chan *domain.MarketSnapshot) worker.Deps {
	return worker.Deps{
		Strategy:   &mockStrategy{},
		Fetcher:    &mockDataFetcher{},
		Executor:   &mockOfferExecutor{},
		SnapshotCh: snapshotCh,
	}
}

// --- Helpers ---

func testServiceConfig() Config {
	return Config{
		GlobalQuota:    90,
		RefillInterval: 1 * time.Minute,
	}
}

func testStrategyConfig() domain.StrategyConfig {
	return domain.StrategyConfig{
		Currency: "fUSD",
		Amount:   domain.AmountConfig{Min: 50, Max: 10000},
		Rate:     domain.RateConfig{Min: 0.0002, Max: 0.005},
		Period:   domain.PeriodConfig{Min: 2, Max: 30},
	}
}

func startedService(t *testing.T) (*Service, context.CancelFunc) {
	t.Helper()
	svc := NewService(&mockDepsFactory{}, testServiceConfig(), quota.NewRateLimiterPool(1000, 1000))

	ctx, cancel := context.WithCancel(context.Background())
	go svc.Start(ctx)

	// Wait for deterministic ready signal
	select {
	case <-svc.Ready():
	case <-time.After(2 * time.Second):
		t.Fatal("service did not become ready in time")
	}
	return svc, cancel
}

// --- Tests ---

func TestService_ReadySignal(t *testing.T) {
	svc := NewService(&mockDepsFactory{}, testServiceConfig(), quota.NewRateLimiterPool(1000, 1000))

	// Before Start, ready channel should not be closed
	select {
	case <-svc.Ready():
		t.Fatal("ready channel should not be closed before Start")
	default:
		// expected
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go svc.Start(ctx)

	// After Start, ready channel should close quickly
	select {
	case <-svc.Ready():
		// expected
	case <-time.After(2 * time.Second):
		t.Fatal("ready channel was not closed after Start")
	}
}

func TestService_NewService(t *testing.T) {
	svc := NewService(&mockDepsFactory{}, testServiceConfig(), quota.NewRateLimiterPool(1000, 1000))
	if svc == nil {
		t.Fatal("expected non-nil service")
	}
	if svc.WorkerCount() != 0 {
		t.Errorf("initial WorkerCount: got %d, want 0", svc.WorkerCount())
	}
}

func TestService_StartAndStop(t *testing.T) {
	svc, cancel := startedService(t)
	defer cancel()

	stopCtx, stopCancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer stopCancel()

	err := svc.Stop(stopCtx)
	if err != nil {
		t.Fatalf("Stop error: %v", err)
	}
}

func TestService_StartWorker(t *testing.T) {
	svc, cancel := startedService(t)
	defer cancel()
	defer svc.Stop(context.Background())

	err := svc.StartWorker(context.Background(),"u1", testStrategyConfig())
	if err != nil {
		t.Fatalf("StartWorker error: %v", err)
	}

	time.Sleep(20 * time.Millisecond)

	if svc.WorkerCount() != 1 {
		t.Errorf("WorkerCount: got %d, want 1", svc.WorkerCount())
	}
}

func TestService_StartWorkerDuplicate(t *testing.T) {
	svc, cancel := startedService(t)
	defer cancel()
	defer svc.Stop(context.Background())

	_ = svc.StartWorker(context.Background(),"u1", testStrategyConfig())
	err := svc.StartWorker(context.Background(),"u1", testStrategyConfig())

	if err == nil {
		t.Fatal("expected error for duplicate StartWorker")
	}
}

func TestService_StopWorker(t *testing.T) {
	svc, cancel := startedService(t)
	defer cancel()
	defer svc.Stop(context.Background())

	_ = svc.StartWorker(context.Background(),"u1", testStrategyConfig())
	time.Sleep(20 * time.Millisecond)

	err := svc.StopWorker(context.Background(),"u1")
	if err != nil {
		t.Fatalf("StopWorker error: %v", err)
	}

	time.Sleep(30 * time.Millisecond)

	if svc.WorkerCount() != 0 {
		t.Errorf("WorkerCount after stop: got %d, want 0", svc.WorkerCount())
	}
}

func TestService_StopWorkerNonExistent(t *testing.T) {
	svc, cancel := startedService(t)
	defer cancel()
	defer svc.Stop(context.Background())

	err := svc.StopWorker(context.Background(),"u1")
	if err == nil {
		t.Fatal("expected error for non-existent StopWorker")
	}
}

func TestService_ReloadConfig(t *testing.T) {
	svc, cancel := startedService(t)
	defer cancel()
	defer svc.Stop(context.Background())

	_ = svc.StartWorker(context.Background(),"u1", testStrategyConfig())
	time.Sleep(20 * time.Millisecond)

	newCfg := testStrategyConfig()
	newCfg.Rate.Min = 0.0005

	err := svc.ReloadConfig(context.Background(),"u1", newCfg)
	if err != nil {
		t.Fatalf("ReloadConfig error: %v", err)
	}
}

func TestService_ReloadConfigNonExistent(t *testing.T) {
	svc, cancel := startedService(t)
	defer cancel()
	defer svc.Stop(context.Background())

	err := svc.ReloadConfig(context.Background(),"u1", testStrategyConfig())
	if err == nil {
		t.Fatal("expected error for non-existent ReloadConfig")
	}
}

func TestService_BroadcastSnapshot(t *testing.T) {
	svc, cancel := startedService(t)
	defer cancel()
	defer svc.Stop(context.Background())

	_ = svc.StartWorker(context.Background(),"u1", testStrategyConfig())
	_ = svc.StartWorker(context.Background(),"u2", testStrategyConfig())
	time.Sleep(20 * time.Millisecond)

	snapshot := &domain.MarketSnapshot{
		Symbol:    "fUSD",
		FRR:       0.0003,
		Timestamp: time.Now(),
	}

	svc.BroadcastSnapshot(snapshot)

	// Verify channels received snapshot
	svc.mu.RLock()
	for userID, ch := range svc.snapshotChs {
		select {
		case s := <-ch:
			if s != snapshot {
				t.Errorf("worker %s got different snapshot", userID)
			}
		default:
			// Channel was consumed by the worker already — that's OK
		}
	}
	svc.mu.RUnlock()
}

func TestService_BroadcastDropsStale(t *testing.T) {
	svc, cancel := startedService(t)
	defer cancel()

	// Manually create a snapshot channel to test drop behavior
	// (worker won't consume it)
	svc.mu.Lock()
	testCh := make(chan *domain.MarketSnapshot, 1)
	svc.snapshotChs["test-user"] = testCh
	svc.mu.Unlock()

	stale := &domain.MarketSnapshot{FRR: 0.0001}
	testCh <- stale

	fresh := &domain.MarketSnapshot{FRR: 0.0005}
	svc.BroadcastSnapshot(fresh)

	received := <-testCh
	if received.FRR != 0.0005 {
		t.Errorf("expected fresh snapshot (FRR=0.0005), got FRR=%f", received.FRR)
	}

	// Cleanup
	svc.mu.Lock()
	delete(svc.snapshotChs, "test-user")
	svc.mu.Unlock()
	close(testCh)

	svc.Stop(context.Background())
}

func TestService_StopAllCleansUp(t *testing.T) {
	svc, cancel := startedService(t)
	defer cancel()

	_ = svc.StartWorker(context.Background(),"u1", testStrategyConfig())
	_ = svc.StartWorker(context.Background(),"u2", testStrategyConfig())
	_ = svc.StartWorker(context.Background(),"u3", testStrategyConfig())

	time.Sleep(20 * time.Millisecond)

	if svc.WorkerCount() != 3 {
		t.Fatalf("WorkerCount: got %d, want 3", svc.WorkerCount())
	}

	stopCtx, stopCancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer stopCancel()

	err := svc.Stop(stopCtx)
	if err != nil {
		t.Fatalf("Stop error: %v", err)
	}

	if svc.WorkerCount() != 0 {
		t.Errorf("WorkerCount after StopAll: got %d, want 0", svc.WorkerCount())
	}

	svc.mu.RLock()
	if len(svc.snapshotChs) != 0 {
		t.Errorf("snapshot channels: got %d, want 0", len(svc.snapshotChs))
	}
	svc.mu.RUnlock()
}

func TestService_SetUserPlan(t *testing.T) {
	svc := NewService(&mockDepsFactory{}, testServiceConfig(), quota.NewRateLimiterPool(1000, 1000))

	svc.SetUserPlan("u1", domain.PlanPro)
	if r := svc.quota.Remaining("u1"); r != quotaPro {
		t.Errorf("pro quota: got %d, want %d", r, quotaPro)
	}

	svc.SetUserPlan("u2", domain.PlanEnterprise)
	if r := svc.quota.Remaining("u2"); r != quotaEnterprise {
		t.Errorf("enterprise quota: got %d, want %d", r, quotaEnterprise)
	}

	svc.SetUserPlan("u3", domain.PlanStarter)
	if r := svc.quota.Remaining("u3"); r != quotaStarter {
		t.Errorf("starter quota: got %d, want %d", r, quotaStarter)
	}
}

func TestService_PlanQuota(t *testing.T) {
	tests := []struct {
		plan string
		want int
	}{
		{domain.PlanStarter, quotaStarter},
		{domain.PlanPro, quotaPro},
		{domain.PlanEnterprise, quotaEnterprise},
		{domain.PlanFree, quotaStarter}, // default
		{"unknown", quotaStarter},       // default
	}
	for _, tt := range tests {
		if got := planQuota(tt.plan); got != tt.want {
			t.Errorf("planQuota(%s): got %d, want %d", tt.plan, got, tt.want)
		}
	}
}

func TestService_MultipleStartStop(t *testing.T) {
	svc, cancel := startedService(t)
	defer cancel()
	defer svc.Stop(context.Background())

	// Start multiple workers
	for i := 0; i < 5; i++ {
		userID := "u" + string(rune('1'+i))
		err := svc.StartWorker(context.Background(),userID, testStrategyConfig())
		if err != nil {
			t.Fatalf("StartWorker(%s) error: %v", userID, err)
		}
	}

	time.Sleep(20 * time.Millisecond)

	if svc.WorkerCount() != 5 {
		t.Errorf("WorkerCount: got %d, want 5", svc.WorkerCount())
	}

	// Stop some workers
	_ = svc.StopWorker(context.Background(),"u1")
	_ = svc.StopWorker(context.Background(),"u3")

	time.Sleep(30 * time.Millisecond)

	if svc.WorkerCount() != 3 {
		t.Errorf("WorkerCount after partial stop: got %d, want 3", svc.WorkerCount())
	}
}

func TestService_BroadcastConcurrent(t *testing.T) {
	svc, cancel := startedService(t)
	defer cancel()
	defer svc.Stop(context.Background())

	_ = svc.StartWorker(context.Background(),"u1", testStrategyConfig())
	_ = svc.StartWorker(context.Background(),"u2", testStrategyConfig())
	time.Sleep(20 * time.Millisecond)

	// Broadcast from multiple goroutines
	var count int64
	done := make(chan struct{})

	for i := 0; i < 10; i++ {
		go func(n int) {
			snapshot := &domain.MarketSnapshot{
				FRR:       float64(n) * 0.0001,
				Timestamp: time.Now(),
			}
			svc.BroadcastSnapshot(snapshot)
			atomic.AddInt64(&count, 1)
			if atomic.LoadInt64(&count) == 10 {
				close(done)
			}
		}(i)
	}

	select {
	case <-done:
		// All broadcasts completed without panic
	case <-time.After(2 * time.Second):
		t.Fatal("concurrent broadcast timed out")
	}
}
