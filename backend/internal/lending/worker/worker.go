package worker

import (
	"context"
	"fmt"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// Strategy computes lending decisions from market + user context.
type Strategy interface {
	Apply(ctx *domain.DecisionContext) *domain.DecisionResult
}

// UserData holds the per-user private data fetched each tick.
type UserData struct {
	Available     float64
	ActiveOffers  []domain.FundingOffer
	ActiveCredits []domain.FundingCredit
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

// Deps holds all dependencies needed by a LendingWorker.
type Deps struct {
	Strategy   Strategy
	Fetcher    DataFetcher
	Executor   OfferExecutor
	SnapshotCh <-chan *domain.MarketSnapshot
}

// LendingWorker implements the Worker interface for a single user.
type LendingWorker struct {
	userID   string
	config   domain.StrategyConfig
	deps     Deps
	lc       *lifecycle
	stopCh   chan struct{}
	configCh chan domain.StrategyConfig
	onError  func(userID string, err interface{}) // optional error callback for testing
}

// NewLendingWorker creates a new LendingWorker.
func NewLendingWorker(userID string, config domain.StrategyConfig, deps Deps) *LendingWorker {
	return &LendingWorker{
		userID:   userID,
		config:   config,
		deps:     deps,
		lc:       newLifecycle(),
		stopCh:   make(chan struct{}),
		configCh: make(chan domain.StrategyConfig, 1),
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

	// Phase 3: Strategy decision
	decision := w.deps.Strategy.Apply(decisionCtx)

	// Phase 4: Execute
	if decision != nil {
		_, execErr := w.deps.Executor.ExecuteDecision(ctx, w.userID, decision)
		if execErr != nil {
			// Execution error — logged by executor, worker continues
			_ = execErr
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
