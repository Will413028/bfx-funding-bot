package infra

import (
	"context"
	"time"

	"github.com/redis/go-redis/v9"
	"go.uber.org/fx"
	"go.uber.org/zap"

	"github.com/will/bfx-funding-bot/backend/internal/appconfig"
)

func NewRedisClient(lc fx.Lifecycle, cfg appconfig.Config, log *zap.Logger) (*redis.Client, error) {
	opts, err := redis.ParseURL(cfg.RedisURL)
	if err != nil {
		return nil, err
	}

	client := redis.NewClient(opts)

	pingCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := client.Ping(pingCtx).Err(); err != nil {
		_ = client.Close()
		return nil, err
	}

	log.Info("connected to Redis")

	lc.Append(fx.Hook{
		OnStop: func(ctx context.Context) error {
			log.Info("closing Redis connection")
			return client.Close()
		},
	})

	return client, nil
}
