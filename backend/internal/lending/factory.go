package lending

import (
	"github.com/will/bfx-funding-bot/backend/internal/bitfinex"
	"github.com/will/bfx-funding-bot/backend/internal/crypto"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/lending/execution"
	"github.com/will/bfx-funding-bot/backend/internal/lending/quota"
	"github.com/will/bfx-funding-bot/backend/internal/lending/strategy"
	"github.com/will/bfx-funding-bot/backend/internal/lending/worker"
	"github.com/will/bfx-funding-bot/backend/internal/repository"
)

// DepsFactory builds worker.Deps for each user. It holds shared infrastructure
// references and creates per-user DataFetcher and OfferExecutor instances.
type DepsFactory struct {
	client      *bitfinex.Client
	cipher      *crypto.AES
	apiKeyRepo  repository.APIKeyRepository
	execRepo    repository.ExecutionRepository
	limiterPool *quota.RateLimiterPool
}

// NewDepsFactory creates a new DepsFactory.
func NewDepsFactory(
	client *bitfinex.Client,
	cipher *crypto.AES,
	apiKeyRepo repository.APIKeyRepository,
	execRepo repository.ExecutionRepository,
	limiterPool *quota.RateLimiterPool,
) *DepsFactory {
	return &DepsFactory{
		client:      client,
		cipher:      cipher,
		apiKeyRepo:  apiKeyRepo,
		execRepo:    execRepo,
		limiterPool: limiterPool,
	}
}

// BuildWorkerDeps creates a complete worker.Deps for the given user.
func (f *DepsFactory) BuildWorkerDeps(userID string, currency string, snapshotCh <-chan *domain.MarketSnapshot) worker.Deps {
	limiter := f.limiterPool.Get(userID)
	fetcher := newUserDataFetcher(f.client, f.apiKeyRepo, f.cipher, currency, limiter)

	offerExecutor := execution.NewOfferExecutor(f.client, f.apiKeyRepo, f.cipher, f.execRepo, limiter)
	executor := newExecutorAdapter(offerExecutor)

	return worker.Deps{
		Strategy:   strategy.NewPricingStrategy(),
		Fetcher:    fetcher,
		Executor:   executor,
		SnapshotCh: snapshotCh,
	}
}

// Compile-time check.
var _ WorkerDepsFactory = (*DepsFactory)(nil)
