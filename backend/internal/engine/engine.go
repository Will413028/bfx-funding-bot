package engine

import (
	"context"
	"sync"
	"time"

	"go.uber.org/zap"

	"github.com/will/bfx-funding-bot/backend/internal/bitfinex"
	"github.com/will/bfx-funding-bot/backend/internal/crypto"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/repository"
)

const tickInterval = 3 * time.Minute

type Engine struct {
	bfx        *bitfinex.Client
	apiKeyRepo repository.APIKeyRepository
	configRepo repository.ConfigRepository
	cipher     *crypto.AES
	log        *zap.Logger

	cancel context.CancelFunc
	wg     sync.WaitGroup
}

func NewEngine(
	bfx *bitfinex.Client,
	apiKeyRepo repository.APIKeyRepository,
	configRepo repository.ConfigRepository,
	cipher *crypto.AES,
	log *zap.Logger,
) *Engine {
	return &Engine{
		bfx:        bfx,
		apiKeyRepo: apiKeyRepo,
		configRepo: configRepo,
		cipher:     cipher,
		log:        log.Named("engine"),
	}
}

func (e *Engine) Start(ctx context.Context) {
	ctx, e.cancel = context.WithCancel(ctx)
	e.wg.Add(1)

	go func() {
		defer e.wg.Done()
		e.log.Info("lending engine started", zap.Duration("interval", tickInterval))

		// Run first tick immediately
		e.tick(ctx)

		ticker := time.NewTicker(tickInterval)
		defer ticker.Stop()

		for {
			select {
			case <-ctx.Done():
				e.log.Info("lending engine stopped")
				return
			case <-ticker.C:
				e.tick(ctx)
			}
		}
	}()
}

func (e *Engine) Stop() {
	if e.cancel != nil {
		e.cancel()
	}
	e.wg.Wait()
}

func (e *Engine) tick(ctx context.Context) {
	e.log.Debug("tick started")

	keys, secrets, err := e.apiKeyRepo.ListVerified(ctx)
	if err != nil {
		e.log.Error("failed to list verified API keys", zap.Error(err))
		return
	}

	if len(keys) == 0 {
		e.log.Debug("no verified API keys found")
		return
	}

	processed := 0
	for i, key := range keys {
		if ctx.Err() != nil {
			break
		}
		e.processUser(ctx, key, secrets[i])
		processed++
	}

	e.log.Debug("tick completed", zap.Int("processed", processed))
}

func (e *Engine) processUser(ctx context.Context, key domain.APIKey, encryptedSecret []byte) {
	log := e.log.With(zap.String("userID", key.UserID))

	// Recover from panics
	defer func() {
		if r := recover(); r != nil {
			log.Error("panic during user processing", zap.Any("panic", r))
		}
	}()

	// Get user config
	config, err := e.configRepo.GetByUserID(ctx, key.UserID)
	if err != nil {
		log.Debug("failed to get user config, skipping", zap.Error(err))
		return
	}
	if config == nil {
		log.Debug("user has no strategy config, skipping")
		return
	}

	// Decrypt API secret
	secret, err := e.cipher.Decrypt(encryptedSecret)
	if err != nil {
		log.Error("failed to decrypt API secret", zap.Error(err))
		return
	}

	// Execute strategy
	ExecuteStrategy(ctx, e.bfx, key.APIKey, string(secret), config.Config, log)
}
