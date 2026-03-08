package repository

import (
	"go.uber.org/fx"

	"github.com/will/bfx-funding-bot/backend/internal/repository/postgres"
	redisrepo "github.com/will/bfx-funding-bot/backend/internal/repository/redis"
)

var Module = fx.Module("repository",
	fx.Options(postgres.Module),
	fx.Options(redisrepo.Module),
	fx.Provide(fx.Annotate(
		func(r *postgres.UserRepo) UserRepository { return r },
		fx.As(new(UserRepository)),
	)),
	fx.Provide(fx.Annotate(
		func(r *postgres.APIKeyRepo) APIKeyRepository { return r },
		fx.As(new(APIKeyRepository)),
	)),
	fx.Provide(fx.Annotate(
		func(r *postgres.ConfigRepo) ConfigRepository { return r },
		fx.As(new(ConfigRepository)),
	)),
	fx.Provide(fx.Annotate(
		func(r *postgres.ExecutionRepo) ExecutionRepository { return r },
		fx.As(new(ExecutionRepository)),
	)),
	fx.Provide(fx.Annotate(
		func(r *postgres.BillingRepo) BillingRepository { return r },
		fx.As(new(BillingRepository)),
	)),
	fx.Provide(fx.Annotate(
		func(r *redisrepo.SnapshotCacheRepo) SnapshotCache { return r },
		fx.As(new(SnapshotCache)),
	)),
	fx.Provide(fx.Annotate(
		func(r *redisrepo.SnapshotPubSubRepo) SnapshotPubSub { return r },
		fx.As(new(SnapshotPubSub)),
	)),
)
