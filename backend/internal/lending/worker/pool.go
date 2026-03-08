package worker

import (
	"context"
	"fmt"
	"sync"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// Worker represents a per-user lending worker managed by the Pool.
// Implemented by the concrete worker in E6; tests use mocks.
type Worker interface {
	// Run starts the worker's main loop. Blocks until ctx is cancelled
	// or an unrecoverable error occurs.
	Run(ctx context.Context) error
	// Stop signals the worker to shut down gracefully.
	Stop()
	// ReloadConfig hot-reloads the strategy configuration.
	ReloadConfig(config domain.StrategyConfig)
	// UserID returns the user ID this worker serves.
	UserID() string
}

// WorkerFactory creates a new Worker for the given user.
type WorkerFactory func(ctx context.Context, userID string, config domain.StrategyConfig) Worker

// entry tracks a running worker and its cancel function.
type entry struct {
	worker Worker
	cancel context.CancelFunc
}

// Pool manages per-user Worker goroutines.
type Pool struct {
	mu      sync.RWMutex
	workers map[string]*entry
	factory WorkerFactory
	rootCtx context.Context
	wg      sync.WaitGroup
}

// NewPool creates a new Worker Pool.
func NewPool(ctx context.Context, factory WorkerFactory) *Pool {
	return &Pool{
		workers: make(map[string]*entry),
		factory: factory,
		rootCtx: ctx,
	}
}

// Start creates and runs a new Worker for the given userID.
// Returns an error if a worker already exists for this user.
func (p *Pool) Start(userID string, config domain.StrategyConfig) error {
	p.mu.Lock()
	defer p.mu.Unlock()

	if _, exists := p.workers[userID]; exists {
		return fmt.Errorf("worker already exists for user %s", userID)
	}

	workerCtx, cancel := context.WithCancel(p.rootCtx)
	w := p.factory(workerCtx, userID, config)

	p.workers[userID] = &entry{
		worker: w,
		cancel: cancel,
	}

	p.wg.Add(1)
	go func() {
		defer p.wg.Done()
		_ = w.Run(workerCtx)
		// Auto-cleanup: remove from map when Run exits
		p.mu.Lock()
		delete(p.workers, userID)
		p.mu.Unlock()
	}()

	return nil
}

// Stop stops the Worker for the given userID and removes it from the pool.
func (p *Pool) Stop(userID string) error {
	p.mu.Lock()
	e, exists := p.workers[userID]
	if !exists {
		p.mu.Unlock()
		return fmt.Errorf("no worker found for user %s", userID)
	}
	// Remove from map immediately to prevent double-stop
	delete(p.workers, userID)
	p.mu.Unlock()

	e.worker.Stop()
	e.cancel()
	return nil
}

// StopAll stops all running Workers and waits for them to finish.
// Respects the provided context for timeout.
func (p *Pool) StopAll(ctx context.Context) error {
	p.mu.Lock()
	entries := make([]*entry, 0, len(p.workers))
	for _, e := range p.workers {
		entries = append(entries, e)
	}
	// Clear map
	p.workers = make(map[string]*entry)
	p.mu.Unlock()

	// Signal all workers to stop
	for _, e := range entries {
		e.worker.Stop()
		e.cancel()
	}

	// Wait for all goroutines to finish, respecting context
	done := make(chan struct{})
	go func() {
		p.wg.Wait()
		close(done)
	}()

	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Reload forwards a new StrategyConfig to the Worker for the given userID.
func (p *Pool) Reload(userID string, config domain.StrategyConfig) error {
	p.mu.RLock()
	defer p.mu.RUnlock()

	e, exists := p.workers[userID]
	if !exists {
		return fmt.Errorf("no worker found for user %s", userID)
	}

	e.worker.ReloadConfig(config)
	return nil
}

// Count returns the number of active Workers.
func (p *Pool) Count() int {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return len(p.workers)
}

// Has returns true if a Worker exists for the given userID.
func (p *Pool) Has(userID string) bool {
	p.mu.RLock()
	defer p.mu.RUnlock()
	_, exists := p.workers[userID]
	return exists
}
