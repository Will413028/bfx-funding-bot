package postgres

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/repository/postgres/sqlc"
)

type APIKeyRepo struct {
	q *sqlc.Queries
}

func NewAPIKeyRepo(q *sqlc.Queries) *APIKeyRepo {
	return &APIKeyRepo{q: q}
}

func (r *APIKeyRepo) Create(ctx context.Context, userID, label, apiKey string, encryptedSecret []byte) (*domain.APIKey, error) {
	uid, err := parseUUID(userID)
	if err != nil {
		return nil, err
	}
	row, err := r.q.CreateAPIKey(ctx, sqlc.CreateAPIKeyParams{
		UserID:    uid,
		Label:     label,
		ApiKey:    apiKey,
		ApiSecret: encryptedSecret,
	})
	if err != nil {
		return nil, err
	}
	return toDomainAPIKey(row), nil
}

func (r *APIKeyRepo) GetByID(ctx context.Context, id string) (*domain.APIKey, []byte, error) {
	uid, err := parseUUID(id)
	if err != nil {
		return nil, nil, err
	}
	row, err := r.q.GetAPIKeyByID(ctx, uid)
	if err != nil {
		return nil, nil, err
	}
	return toDomainAPIKey(row), row.ApiSecret, nil
}

func (r *APIKeyRepo) GetByUserID(ctx context.Context, userID string) (*domain.APIKey, []byte, error) {
	uid, err := parseUUID(userID)
	if err != nil {
		return nil, nil, err
	}
	row, err := r.q.GetAPIKeyByUserID(ctx, uid)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil, nil
		}
		return nil, nil, err
	}
	return toDomainAPIKey(row), row.ApiSecret, nil
}

func (r *APIKeyRepo) Delete(ctx context.Context, id, userID string) error {
	keyID, err := parseUUID(id)
	if err != nil {
		return err
	}
	uid, err := parseUUID(userID)
	if err != nil {
		return err
	}
	rows, err := r.q.DeleteAPIKey(ctx, sqlc.DeleteAPIKeyParams{
		ID:     keyID,
		UserID: uid,
	})
	if err != nil {
		return err
	}
	if rows == 0 {
		return domain.ErrAPIKeyNotFound()
	}
	return nil
}

func toDomainAPIKey(row sqlc.ApiKey) *domain.APIKey {
	return &domain.APIKey{
		ID:        uuidToString(row.ID),
		UserID:    uuidToString(row.UserID),
		Label:     row.Label,
		APIKey:    row.ApiKey,
		CreatedAt: row.CreatedAt.Time,
		UpdatedAt: row.UpdatedAt.Time,
	}
}
