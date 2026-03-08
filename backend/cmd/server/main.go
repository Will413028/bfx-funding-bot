package main

import (
	"context"
	"fmt"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"go.uber.org/fx"
	"go.uber.org/fx/fxevent"
	"go.uber.org/zap"

	"github.com/will/bfx-funding-bot/backend/internal/appconfig"
	"github.com/will/bfx-funding-bot/backend/internal/auth"
	"github.com/will/bfx-funding-bot/backend/internal/bitfinex"
	"github.com/will/bfx-funding-bot/backend/internal/crypto"
	"github.com/will/bfx-funding-bot/backend/internal/engine"
	"github.com/will/bfx-funding-bot/backend/internal/handler"
	"github.com/will/bfx-funding-bot/backend/internal/infra"
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
		fx.Provide(service.NewUserService),
		fx.Provide(service.NewAPIKeyService),
		fx.Provide(service.NewConfigService),
		fx.Provide(service.NewDashboardService),
		fx.Provide(engine.NewEngine),
		fx.Provide(handler.NewAuthHandler),
		fx.Provide(handler.NewAPIKeyHandler),
		fx.Provide(handler.NewConfigHandler),
		fx.Provide(handler.NewDashboardHandler),
		fx.Provide(handler.NewHealthHandler),
		fx.Provide(handler.NewRouter),
		fx.WithLogger(func(log *zap.Logger) fxevent.Logger {
			return &fxevent.ZapLogger{Logger: log}
		}),
		fx.Invoke(startServer),
		fx.Invoke(startEngine),
	).Run()
}

func newLogger(cfg appconfig.Config) (*zap.Logger, error) {
	if cfg.Environment == "production" {
		return zap.NewProduction()
	}
	return zap.NewDevelopment()
}

func startEngine(lc fx.Lifecycle, e *engine.Engine, log *zap.Logger) {
	lc.Append(fx.Hook{
		OnStart: func(ctx context.Context) error {
			e.Start(ctx)
			return nil
		},
		OnStop: func(ctx context.Context) error {
			e.Stop()
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
