package lending

import (
	"context"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/lending/execution"
	"github.com/will/bfx-funding-bot/backend/internal/lending/worker"
)

// executorAdapter bridges execution.OfferExecutor → worker.OfferExecutor.
// This avoids worker/ importing execution/ directly.
type executorAdapter struct {
	inner *execution.OfferExecutor
}

func newExecutorAdapter(inner *execution.OfferExecutor) *executorAdapter {
	return &executorAdapter{inner: inner}
}

func (a *executorAdapter) ExecuteDecision(ctx context.Context, userID string, decision *domain.DecisionResult) (*worker.ExecutionSummary, error) {
	result, err := a.inner.ExecuteDecision(ctx, userID, decision)
	if err != nil {
		return nil, err
	}
	return &worker.ExecutionSummary{
		CancelOK:   result.CancelOK,
		CancelFail: result.CancelFail,
		PlaceOK:    result.PlaceOK,
		PlaceFail:  result.PlaceFail,
	}, nil
}

// Compile-time check.
var _ worker.OfferExecutor = (*executorAdapter)(nil)
