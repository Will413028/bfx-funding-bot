package worker

import (
	"context"
	"fmt"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// Cold Start Protocol (§6.4) phase durations and MDC weights.
const (
	coldStartPhase1Duration = 10 * time.Minute
	coldStartPhase2Duration = 10 * time.Minute
	coldStartPhase3Duration = 10 * time.Minute
	coldStartTotalDuration  = coldStartPhase1Duration + coldStartPhase2Duration + coldStartPhase3Duration

	coldStartPhase1MDC = 0.0  // pure FRR following
	coldStartPhase2MDC = 0.50 // VWAP activated
	coldStartPhase3MDC = 0.75 // advanced signals activated
)

// Strategy computes lending decisions from market + user context.
type Strategy interface {
	Apply(ctx *domain.DecisionContext) *domain.DecisionResult
}

// UserData holds the per-user private data fetched each tick.
type UserData struct {
	ActiveOffers  []domain.FundingOffer
	ActiveCredits []domain.FundingCredit
	Available     float64
}

// DataFetcher retrieves per-user private data from the exchange.
type DataFetcher interface {
	FetchUserData(ctx context.Context, userID string) (*UserData, error)
}

// ExecutionSummary mirrors execution.ExecutionSummary to avoid import.
type ExecutionSummary struct {
	CancelOK   int
	CancelFail int
	PlaceOK    int
	PlaceFail  int
}

// OfferExecutor executes strategy decisions via the exchange API.
type OfferExecutor interface {
	ExecuteDecision(ctx context.Context, userID string, decision *domain.DecisionResult) (*ExecutionSummary, error)
}

// QuotaAcquirer checks if API call budget is available.
type QuotaAcquirer interface {
	Acquire(userID string, n int) bool
}

// Deps holds all dependencies needed by a LendingWorker.
type Deps struct {
	Strategy   Strategy
	Fetcher    DataFetcher
	Executor   OfferExecutor
	Quota      QuotaAcquirer
	SnapshotCh <-chan *domain.MarketSnapshot
}

// LendingWorker implements the Worker interface for a single user.
type LendingWorker struct {
	deps       Deps
	lc         *lifecycle
	stopCh     chan struct{}
	configCh   chan domain.StrategyConfig
	startedAt  time.Time                            // Cold Start Protocol (§6.4): tracks worker startup time
	lastLentAt time.Time                            // G11: tracks last successful offer for idle urgency
	onError    func(userID string, err interface{}) // optional error callback for testing
	nowFn      func() time.Time                     // injectable clock for testing
	userID     string
	config     domain.StrategyConfig
}

// NewLendingWorker creates a new LendingWorker.
func NewLendingWorker(userID string, config domain.StrategyConfig, deps Deps) *LendingWorker {
	return &LendingWorker{
		userID:    userID,
		config:    config,
		deps:      deps,
		lc:        newLifecycle(),
		stopCh:    make(chan struct{}),
		configCh:  make(chan domain.StrategyConfig, 1),
		startedAt: time.Now(),
		nowFn:     time.Now,
	}
}

// Run starts the worker's main loop. Blocks until ctx is cancelled or Stop is called.
func (w *LendingWorker) Run(ctx context.Context) error {
	w.lc.transition(StateRunning)
	defer w.lc.transition(StateStopped)

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-w.stopCh:
			return nil
		case newCfg := <-w.configCh:
			w.config = newCfg
		case snapshot, ok := <-w.deps.SnapshotCh:
			if !ok {
				return nil // channel closed
			}
			// Drain any pending config reload before tick
			select {
			case newCfg := <-w.configCh:
				w.config = newCfg
			default:
			}
			w.tick(ctx, snapshot)
		}
	}
}

// Stop signals the worker to shut down gracefully.
func (w *LendingWorker) Stop() {
	w.lc.transition(StateStopping)
	select {
	case <-w.stopCh:
		// already closed
	default:
		close(w.stopCh)
	}
}

// ReloadConfig updates the worker's config for the next tick. Non-blocking.
func (w *LendingWorker) ReloadConfig(config domain.StrategyConfig) {
	// Drain old value if present
	select {
	case <-w.configCh:
	default:
	}
	// Send new config (non-blocking since buffer=1 and we just drained)
	select {
	case w.configCh <- config:
	default:
	}
}

// UserID returns the user ID this worker serves.
func (w *LendingWorker) UserID() string {
	return w.userID
}

// State returns the current lifecycle state.
func (w *LendingWorker) State() WorkerState {
	return w.lc.State()
}

// ColdStartPhase returns the current cold start phase (1-4).
// Phase 1: pure FRR (0-10min), Phase 2: +VWAP (10-20min),
// Phase 3: +advanced (20-30min), Phase 4: full strategy (30min+).
func (w *LendingWorker) ColdStartPhase() int {
	elapsed := w.nowFn().Sub(w.startedAt)
	switch {
	case elapsed < coldStartPhase1Duration:
		return 1
	case elapsed < coldStartPhase1Duration+coldStartPhase2Duration:
		return 2
	case elapsed < coldStartTotalDuration:
		return 3
	default:
		return 4
	}
}

// applyColdStart creates a modified copy of the snapshot with scaled MDC
// based on the current cold start phase.
func (w *LendingWorker) applyColdStart(snapshot *domain.MarketSnapshot) *domain.MarketSnapshot {
	phase := w.ColdStartPhase()
	if phase >= 4 {
		return snapshot // full strategy mode
	}

	// Shallow copy to avoid mutating shared snapshot
	modified := *snapshot

	var mdcWeight float64
	switch phase {
	case 1:
		mdcWeight = coldStartPhase1MDC
	case 2:
		mdcWeight = coldStartPhase2MDC
	case 3:
		mdcWeight = coldStartPhase3MDC
	}

	modified.MDC = domain.MDCResult{
		Score:          snapshot.MDC.Score * mdcWeight,
		DemandPressure: snapshot.MDC.DemandPressure,
		SupplyPressure: snapshot.MDC.SupplyPressure,
		Timestamp:      snapshot.MDC.Timestamp,
	}

	return &modified
}

// tick executes one cycle: fetch data → build context → strategy → execute.
// Panics are recovered to keep the worker alive.
func (w *LendingWorker) tick(ctx context.Context, snapshot *domain.MarketSnapshot) {
	defer func() {
		if r := recover(); r != nil {
			if w.onError != nil {
				w.onError(w.userID, r)
			}
		}
	}()

	// Phase 0: Check quota (nil-safe for tests without quota)
	if w.deps.Quota != nil && !w.deps.Quota.Acquire(w.userID, 3) {
		return // skip tick, quota exhausted
	}

	// Cold Start Protocol (§6.4): scale MDC based on startup phase
	snapshot = w.applyColdStart(snapshot)

	// Phase 1: Fetch private data
	userData, err := w.deps.Fetcher.FetchUserData(ctx, w.userID)
	if err != nil {
		return // skip tick on fetch error
	}

	// Phase 2: Build DecisionContext
	decisionCtx := &domain.DecisionContext{
		Snapshot:      snapshot,
		Config:        &w.config,
		ActiveOffers:  userData.ActiveOffers,
		ActiveCredits: userData.ActiveCredits,
		Available:     userData.Available,
		Currency:      w.config.Currency,
	}

	// G11: Compute idle minutes
	if !w.lastLentAt.IsZero() {
		decisionCtx.IdleMinutes = time.Since(w.lastLentAt).Minutes()
	}

	// Phase 3: Strategy decision
	decision := w.deps.Strategy.Apply(decisionCtx)

	// Phase 4: Execute
	if decision != nil {
		decision.Currency = w.config.Currency
		_, execErr := w.deps.Executor.ExecuteDecision(ctx, w.userID, decision)
		if execErr != nil {
			// Execution error — logged by executor, worker continues
			_ = execErr
		}
		// G11: Update lastLentAt on successful execution
		if execErr == nil && len(decision.Offers) > 0 {
			w.lastLentAt = time.Now()
		}
	}
}

// Verify LendingWorker satisfies Worker interface at compile time.
var _ Worker = (*LendingWorker)(nil)

// tickCount is exposed for testing — returns number of ticks executed.
// This is a simple approach; production would use metrics.
func (w *LendingWorker) String() string {
	return fmt.Sprintf("LendingWorker[%s, state=%s]", w.userID, w.lc.State())
}
