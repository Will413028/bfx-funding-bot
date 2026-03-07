package infra

import (
	"context"

	"github.com/jackc/pgx/v5/pgxpool"
	"go.uber.org/fx"
	"go.uber.org/zap"

	"github.com/will/bfx-funding-bot/backend/internal/appconfig"
)

func NewPostgresPool(lc fx.Lifecycle, cfg appconfig.Config, log *zap.Logger) (*pgxpool.Pool, error) {
	pool, err := pgxpool.New(context.Background(), cfg.DatabaseURL)
	if err != nil {
		return nil, err
	}

	if err := pool.Ping(context.Background()); err != nil {
		pool.Close()
		return nil, err
	}

	log.Info("connected to PostgreSQL")

	lc.Append(fx.Hook{
		OnStop: func(ctx context.Context) error {
			log.Info("closing PostgreSQL connection pool")
			pool.Close()
			return nil
		},
	})

	return pool, nil
}
