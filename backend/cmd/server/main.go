package main

import (
	"context"
	"fmt"
	"net/http"
	"time"

	adapter "github.com/axiomhq/axiom-go/adapters/zap"
	"github.com/gin-gonic/gin"
	"go.uber.org/fx"
	"go.uber.org/fx/fxevent"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"

	"github.com/will/bfx-funding-bot/backend/internal/appconfig"
	"github.com/will/bfx-funding-bot/backend/internal/auth"
	"github.com/will/bfx-funding-bot/backend/internal/bitfinex"
	"github.com/will/bfx-funding-bot/backend/internal/crypto"
	"github.com/will/bfx-funding-bot/backend/internal/handler"
	"github.com/will/bfx-funding-bot/backend/internal/infra"
	"github.com/will/bfx-funding-bot/backend/internal/lending"
	"github.com/will/bfx-funding-bot/backend/internal/lending/marketfeed"
	"github.com/will/bfx-funding-bot/backend/internal/lending/quota"
	"github.com/will/bfx-funding-bot/backend/internal/lending/signal"
	"github.com/will/bfx-funding-bot/backend/internal/notification"
	"github.com/will/bfx-funding-bot/backend/internal/repository"
	"github.com/will/bfx-funding-bot/backend/internal/service"
)

func main() {
	fx.New(
		appconfig.Module,
		infra.Module,
		repository.Module,
		fx.Provide(newLogger),
		fx.Provide(func(cfg appconfig.Config) *auth.JWTManager {
			return auth.NewJWTManager(cfg.JWTPrivateKey, cfg.JWTPublicKey)
		}),
		fx.Provide(func(cfg appconfig.Config) (*crypto.AES, error) {
			return crypto.NewAES(cfg.AESKey)
		}),
		fx.Provide(func() *bitfinex.Client {
			return bitfinex.NewClient(&http.Client{Timeout: 30 * time.Second})
		}),

		// Lending engine
		fx.Provide(func() *quota.RateLimiterPool {
			// 90 req/min ≈ 1.5 req/sec per user, burst of 3 for parallel fetches
			return quota.NewRateLimiterPool(1.5, 3)
		}),
		fx.Provide(lending.NewDepsFactory),
		fx.Provide(newLendingService),
		fx.Provide(newMarketFeedService),

		// Service layer (with fx.As for interface injection)
		fx.Provide(service.NewUserService),
		fx.Provide(service.NewAPIKeyService),
		fx.Provide(service.NewConfigService),
		fx.Provide(service.NewDashboardService),
		fx.Provide(service.NewEarningsService),
		fx.Provide(service.NewExecutionService),
		fx.Provide(service.NewBillingService),
		fx.Provide(func(cfg appconfig.Config) notification.Notifier {
			return notification.NewResendNotifier(cfg.ResendAPIKey, cfg.NotificationFromEmail)
		}),

		// WebSocket
		fx.Provide(auth.NewWSTokenManager),
		fx.Provide(handler.NewWSTokenHandler),
		fx.Provide(handler.NewHub),

		// Transport layer
		fx.Provide(handler.NewAuthHandler),
		fx.Provide(handler.NewUserHandler),
		fx.Provide(handler.NewAPIKeyHandler),
		fx.Provide(handler.NewConfigHandler),
		fx.Provide(handler.NewDashboardHandler),
		fx.Provide(handler.NewEarningsHandler),
		fx.Provide(handler.NewExecutionHandler),
		fx.Provide(handler.NewBillingHandler),
		fx.Provide(func(svc *lending.Service) handler.EngineHealthProvider { return svc }),
		fx.Provide(handler.NewHealthHandler),
		fx.Provide(handler.NewRouter),
		fx.WithLogger(func(log *zap.Logger) fxevent.Logger {
			return &fxevent.ZapLogger{Logger: log}
		}),
		fx.Invoke(startServer),
		fx.Invoke(startLendingEngine),
		fx.Invoke(startWSHub),
	).Run()
}

func newLogger(lc fx.Lifecycle, cfg appconfig.Config) (*zap.Logger, error) {
	var baseLogger *zap.Logger
	var err error
	if cfg.Environment == "production" {
		baseLogger, err = zap.NewProduction()
	} else {
		baseLogger, err = zap.NewDevelopment()
	}
	if err != nil {
		return nil, err
	}

	// If Axiom is configured, tee logs to both stdout and Axiom
	if cfg.AxiomToken != "" && cfg.AxiomDataset != "" {
		axiomCore, axiomErr := adapter.New(
			adapter.SetDataset(cfg.AxiomDataset),
		)
		if axiomErr != nil {
			baseLogger.Warn("failed to initialize Axiom adapter, using stdout only", zap.Error(axiomErr))
			return baseLogger, nil
		}

		teeCore := zapcore.NewTee(baseLogger.Core(), axiomCore)
		logger := zap.New(teeCore, zap.AddCaller(), zap.AddStacktrace(zap.ErrorLevel))

		lc.Append(fx.Hook{
			OnStop: func(ctx context.Context) error {
				_ = axiomCore.Sync()
				return nil
			},
		})

		logger.Info("axiom logging enabled", zap.String("dataset", cfg.AxiomDataset))
		return logger, nil
	}

	baseLogger.Info("axiom logging disabled (AXIOM_TOKEN or AXIOM_DATASET not set)")
	return baseLogger, nil
}

// newLendingService creates *lending.Service and also provides it as
// service.WorkerManager and service.ConfigReloader via fx.
func newLendingService(factory *lending.DepsFactory, limiterPool *quota.RateLimiterPool) (*lending.Service, service.WorkerManager, service.ConfigReloader) {
	svc := lending.NewService(factory, lending.DefaultConfig(), limiterPool)
	return svc, svc, svc
}

// newMarketFeedService creates a MarketFeed service with all signal sources.
func newMarketFeedService(log *zap.Logger, cache repository.SnapshotCache, pubsub repository.SnapshotPubSub) *marketfeed.Service {
	cfg := marketfeed.Config{
		Symbols: []string{"fUSD"},
	}

	sources := []marketfeed.SignalSource{
		signal.NewBookConsumption(),
		signal.NewLiquidationCascade(),
		signal.NewMomentum(),
		signal.NewMarginUsage(),
		signal.NewCrossCurrency(),
		signal.NewIntraday(),
	}

	return marketfeed.NewService(log, cfg, cache, pubsub, sources, nil)
}

func startLendingEngine(
	lc fx.Lifecycle,
	lendingSvc *lending.Service,
	mfSvc *marketfeed.Service,
	apiKeyRepo repository.APIKeyRepository,
	configRepo repository.ConfigRepository,
	cipher *crypto.AES,
	log *zap.Logger,
) {
	var engineCtx context.Context
	var engineCancel context.CancelFunc

	lc.Append(fx.Hook{
		OnStart: func(ctx context.Context) error {
			log.Info("starting lending engine")

			// Create a long-lived context for engine goroutines.
			// The fx start ctx has a short timeout and must not be used
			// for goroutines that outlive the start phase.
			engineCtx, engineCancel = context.WithCancel(context.Background())

			// Start market feed (WS connection + snapshot loop)
			if err := mfSvc.Start(ctx); err != nil {
				log.Error("market feed start failed", zap.Error(err))
				// Non-fatal: engine runs without live market data
			}

			// Start lending service (pool + quota refill)
			go lendingSvc.Start(engineCtx)

			// Wait for service to initialize (deterministic)
			select {
			case <-lendingSvc.Ready():
				log.Info("lending service ready")
			case <-time.After(5 * time.Second):
				log.Warn("lending service readiness timeout, proceeding anyway")
			}

			// Resume workers for existing users with verified keys + configs
			go resumeWorkers(engineCtx, lendingSvc, apiKeyRepo, configRepo, cipher, log)

			// Forward snapshots from marketfeed to lending service
			go forwardSnapshots(engineCtx, mfSvc, lendingSvc, log)

			return nil
		},
		OnStop: func(ctx context.Context) error {
			log.Info("stopping lending engine")

			engineCancel()

			if err := lendingSvc.Stop(ctx); err != nil {
				log.Error("lending service stop error", zap.Error(err))
			}
			mfSvc.Stop()

			return nil
		},
	})
}

// resumeWorkers scans DB for users with verified keys + configs and starts workers.
func resumeWorkers(
	ctx context.Context,
	svc *lending.Service,
	apiKeyRepo repository.APIKeyRepository,
	configRepo repository.ConfigRepository,
	cipher *crypto.AES,
	log *zap.Logger,
) {
	keys, _, err := apiKeyRepo.ListVerified(ctx)
	if err != nil {
		log.Error("failed to list verified keys for resume", zap.Error(err))
		return
	}

	started := 0
	for _, key := range keys {
		uc, err := configRepo.GetByUserID(ctx, key.UserID)
		if err != nil || uc == nil {
			continue
		}

		if err := svc.StartWorker(ctx, key.UserID, uc.Config); err != nil {
			log.Warn("failed to resume worker",
				zap.String("userID", key.UserID),
				zap.Error(err),
			)
			continue
		}
		started++
	}

	if started > 0 {
		log.Info("resumed workers", zap.Int("count", started))
	}
}

// forwardSnapshots reads from marketfeed output and broadcasts to all workers.
func forwardSnapshots(
	ctx context.Context,
	mfSvc *marketfeed.Service,
	lendingSvc *lending.Service,
	log *zap.Logger,
) {
	ch := mfSvc.Snapshots()
	for {
		select {
		case <-ctx.Done():
			return
		case snap, ok := <-ch:
			if !ok {
				return
			}
			lendingSvc.BroadcastSnapshot(snap)
		}
	}
}

func startWSHub(lc fx.Lifecycle, hub *handler.Hub, log *zap.Logger) {
	lc.Append(fx.Hook{
		OnStart: func(ctx context.Context) error {
			log.Info("starting WebSocket hub")
			hub.Run(ctx)
			return nil
		},
		OnStop: func(ctx context.Context) error {
			log.Info("stopping WebSocket hub")
			hub.Close()
			return nil
		},
	})
}

func startServer(lc fx.Lifecycle, cfg appconfig.Config, router *gin.Engine, log *zap.Logger) {
	srv := &http.Server{
		Addr:    fmt.Sprintf(":%s", cfg.Port),
		Handler: router,
	}

	lc.Append(fx.Hook{
		OnStart: func(ctx context.Context) error {
			go func() {
				log.Info("server starting", zap.String("addr", srv.Addr))
				if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
					log.Fatal("server failed", zap.Error(err))
				}
			}()
			return nil
		},
		OnStop: func(ctx context.Context) error {
			log.Info("server shutting down")
			ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
			defer cancel()
			return srv.Shutdown(ctx)
		},
	})
}
