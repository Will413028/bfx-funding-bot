package infra

import (
	"context"

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

	if err := client.Ping(context.Background()).Err(); err != nil {
		client.Close()
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
