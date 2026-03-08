package postgres

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/jackc/pgx/v5"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/repository/postgres/sqlc"
)

type ConfigRepo struct {
	q *sqlc.Queries
}

func NewConfigRepo(q *sqlc.Queries) *ConfigRepo {
	return &ConfigRepo{q: q}
}

func (r *ConfigRepo) Upsert(ctx context.Context, userID string, configJSON []byte) (*domain.UserConfig, error) {
	uid, err := parseUUID(userID)
	if err != nil {
		return nil, err
	}
	row, err := r.q.UpsertConfig(ctx, sqlc.UpsertConfigParams{
		UserID: uid,
		Config: configJSON,
	})
	if err != nil {
		return nil, err
	}
	return toDomainUserConfig(row)
}

func (r *ConfigRepo) GetByUserID(ctx context.Context, userID string) (*domain.UserConfig, error) {
	uid, err := parseUUID(userID)
	if err != nil {
		return nil, err
	}
	row, err := r.q.GetConfigByUserID(ctx, uid)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil
		}
		return nil, err
	}
	return toDomainUserConfig(row)
}

func (r *ConfigRepo) DeleteByUserID(ctx context.Context, userID string) error {
	uid, err := parseUUID(userID)
	if err != nil {
		return err
	}
	rows, err := r.q.DeleteConfigByUserID(ctx, uid)
	if err != nil {
		return err
	}
	if rows == 0 {
		return domain.ErrConfigNotFound()
	}
	return nil
}

func toDomainUserConfig(row sqlc.UserConfig) (*domain.UserConfig, error) {
	var cfg domain.StrategyConfig
	if err := json.Unmarshal(row.Config, &cfg); err != nil {
		return nil, err
	}
	return &domain.UserConfig{
		ID:        uuidToString(row.ID),
		UserID:    uuidToString(row.UserID),
		Config:    cfg,
		CreatedAt: row.CreatedAt.Time,
		UpdatedAt: row.UpdatedAt.Time,
	}, nil
}
