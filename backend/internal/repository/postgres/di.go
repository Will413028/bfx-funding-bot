package postgres

import (
	"github.com/jackc/pgx/v5/pgxpool"
	"go.uber.org/fx"

	"github.com/will/bfx-funding-bot/backend/internal/repository/postgres/sqlc"
)

var Module = fx.Module("repository.postgres",
	fx.Provide(func(pool *pgxpool.Pool) *sqlc.Queries {
		return sqlc.New(pool)
	}),
	fx.Provide(NewUserRepo),
	fx.Provide(NewAPIKeyRepo),
	fx.Provide(NewConfigRepo),
	fx.Provide(NewExecutionRepo),
	fx.Provide(NewBillingRepo),
)
