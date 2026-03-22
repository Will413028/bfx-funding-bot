package lending

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/lending/quota"
	"github.com/will/bfx-funding-bot/backend/internal/lending/worker"
)

const (
	defaultGlobalQuota  = 90 // requests per minute
	quotaRefillInterval = 1 * time.Minute

	quotaStarter    = 15
	quotaPro        = 30
	quotaEnterprise = 45
)

// WorkerDepsFactory creates worker dependencies for a given user.
// This allows the Service to inject concrete Strategy, DataFetcher,
// and OfferExecutor implementations without importing those packages.
type WorkerDepsFactory interface {
	BuildWorkerDeps(userID string, currency string, snapshotCh <-chan *domain.MarketSnapshot) worker.Deps
}

// Config holds Service-level configuration.
type Config struct {
	GlobalQuota    int
	RefillInterval time.Duration
}

// DefaultConfig returns sensible defaults.
func DefaultConfig() Config {
	return Config{
		GlobalQuota:    defaultGlobalQuota,
		RefillInterval: quotaRefillInterval,
	}
}

// Service is the top-level lending engine orchestrator.
// It integrates worker.Pool, quota.Allocator, and snapshot broadcasting.
// Satisfies service.WorkerManager and service.ConfigReloader interfaces.
type Service struct {
	depsFactory  WorkerDepsFactory
	pool         *worker.Pool
	quota        *quota.Allocator
	limiterPool  *quota.RateLimiterPool
	snapshotChs  map[string]chan *domain.MarketSnapshot
	cancelRefill context.CancelFunc
	ready        chan struct{}
	config       Config
	mu           sync.RWMutex
	readyOnce    sync.Once
}

// NewService creates a new lending engine Service.
func NewService(depsFactory WorkerDepsFactory, cfg Config, limiterPool *quota.RateLimiterPool) *Service {
	svc := &Service{
		quota:       quota.NewAllocator(cfg.GlobalQuota),
		limiterPool: limiterPool,
		depsFactory: depsFactory,
		config:      cfg,
		snapshotChs: make(map[string]chan *domain.MarketSnapshot),
		ready:       make(chan struct{}),
	}
	return svc
}

// Start initializes the pool, starts quota refill, and blocks until ctx is cancelled.
func (s *Service) Start(ctx context.Context) error {
	// Initialize pool (must happen before any StartWorker calls)
	s.mu.Lock()
	s.pool = worker.NewPool(ctx, s.workerFactory)
	refillCtx, cancelRefill := context.WithCancel(ctx)
	s.cancelRefill = cancelRefill
	s.mu.Unlock()

	// Start quota refill
	s.quota.StartRefill(refillCtx, s.config.RefillInterval)

	// Signal readiness (safe against double-call via sync.Once)
	s.readyOnce.Do(func() { close(s.ready) })

	// Block until context is cancelled
	<-ctx.Done()

	// Cleanup
	cancelRefill()
	return nil
}

// Ready returns a channel that is closed when the service has completed initialization.
func (s *Service) Ready() <-chan struct{} {
	return s.ready
}

// Stop gracefully shuts down: stop all workers, then clean up.
func (s *Service) Stop(ctx context.Context) error {
	s.mu.RLock()
	pool := s.pool
	cancelRefill := s.cancelRefill
	s.mu.RUnlock()

	if pool == nil {
		return nil
	}

	err := pool.StopAll(ctx)

	// Close all snapshot channels
	s.mu.Lock()
	for userID, ch := range s.snapshotChs {
		close(ch)
		delete(s.snapshotChs, userID)
	}
	s.mu.Unlock()

	if cancelRefill != nil {
		cancelRefill()
	}

	return err
}

// StartWorker creates and starts a Worker for the given user.
// Sets quota based on plan and adds to pool.
func (s *Service) StartWorker(_ context.Context, userID string, cfg domain.StrategyConfig) error {
	s.mu.Lock()
	pool := s.pool
	if pool == nil {
		s.mu.Unlock()
		return fmt.Errorf("service not started")
	}

	// Create snapshot channel for this worker
	if _, exists := s.snapshotChs[userID]; exists {
		s.mu.Unlock()
		return fmt.Errorf("worker already exists for user %s", userID)
	}
	snapshotCh := make(chan *domain.MarketSnapshot, 1)
	s.snapshotChs[userID] = snapshotCh
	s.mu.Unlock()

	// Set quota based on plan (derived from config or default to starter)
	s.quota.SetUserQuota(userID, quotaStarter)

	// Ensure rate limiter exists for this user
	s.limiterPool.Get(userID)

	// Start worker in pool
	err := pool.Start(userID, cfg)
	if err != nil {
		// Cleanup on failure
		s.mu.Lock()
		delete(s.snapshotChs, userID)
		s.mu.Unlock()
		close(snapshotCh)
		s.quota.RemoveUser(userID)
		return err
	}

	return nil
}

// StopWorker stops the Worker for the given user and cleans up resources.
func (s *Service) StopWorker(_ context.Context, userID string) error {
	s.mu.RLock()
	pool := s.pool
	s.mu.RUnlock()

	if pool == nil {
		return fmt.Errorf("service not started")
	}

	err := pool.Stop(userID)
	if err != nil {
		return err
	}

	// Clean up quota, rate limiter, and snapshot channel
	s.quota.RemoveUser(userID)
	s.limiterPool.Remove(userID)

	s.mu.Lock()
	if ch, exists := s.snapshotChs[userID]; exists {
		close(ch)
		delete(s.snapshotChs, userID)
	}
	s.mu.Unlock()

	return nil
}

// ReloadConfig forwards a new StrategyConfig to the Worker.
func (s *Service) ReloadConfig(_ context.Context, userID string, cfg domain.StrategyConfig) error {
	s.mu.RLock()
	pool := s.pool
	s.mu.RUnlock()

	if pool == nil {
		return fmt.Errorf("service not started")
	}
	return pool.Reload(userID, cfg)
}

// BroadcastSnapshot sends a MarketSnapshot to all active workers.
// Non-blocking: drops stale snapshots if a worker's channel is full.
func (s *Service) BroadcastSnapshot(snapshot *domain.MarketSnapshot) {
	s.mu.RLock()
	defer s.mu.RUnlock()

	for _, ch := range s.snapshotChs {
		// Non-blocking send: drop stale, write new
		select {
		case <-ch:
			// drained stale
		default:
		}
		select {
		case ch <- snapshot:
		default:
		}
	}
}

// SetUserPlan updates the per-user API quota based on subscription plan.
func (s *Service) SetUserPlan(userID string, plan string) {
	q := planQuota(plan)
	s.quota.SetUserQuota(userID, q)
}

// Status returns the current operational status of the lending engine.
func (s *Service) Status() domain.EngineStatus {
	s.mu.RLock()
	running := s.pool != nil
	s.mu.RUnlock()

	return domain.EngineStatus{
		Running:     running,
		WorkerCount: s.WorkerCount(),
	}
}

// WorkerCount returns the number of active workers.
func (s *Service) WorkerCount() int {
	s.mu.RLock()
	pool := s.pool
	s.mu.RUnlock()

	if pool == nil {
		return 0
	}
	return pool.Count()
}

// workerFactory is the WorkerFactory passed to the Pool. It creates
// a LendingWorker with the correct dependencies.
func (s *Service) workerFactory(ctx context.Context, userID string, config domain.StrategyConfig) worker.Worker {
	s.mu.RLock()
	snapshotCh := s.snapshotChs[userID]
	s.mu.RUnlock()

	var ch <-chan *domain.MarketSnapshot
	if snapshotCh != nil {
		ch = snapshotCh
	} else {
		// Fallback: create a dummy channel (shouldn't happen in normal flow)
		dummy := make(chan *domain.MarketSnapshot)
		ch = dummy
	}

	deps := s.depsFactory.BuildWorkerDeps(userID, config.Currency, ch)
	deps.Quota = s.quota
	return worker.NewLendingWorker(userID, config, deps)
}

// planQuota maps subscription plan to per-user API quota per minute.
func planQuota(plan string) int {
	switch plan {
	case domain.PlanEnterprise:
		return quotaEnterprise
	case domain.PlanPro:
		return quotaPro
	case domain.PlanStarter:
		return quotaStarter
	default:
		return quotaStarter
	}
}
