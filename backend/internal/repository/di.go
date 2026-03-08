package repository

import (
	"go.uber.org/fx"

	"github.com/will/bfx-funding-bot/backend/internal/repository/postgres"
)

var Module = fx.Module("repository",
	fx.Options(postgres.Module),
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
)
