package repository

import (
	"context"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

type UserRepository interface {
	Create(ctx context.Context, email, passwordHash string) (*domain.User, error)
	GetByID(ctx context.Context, id string) (*domain.User, error)
	GetByEmail(ctx context.Context, email string) (*domain.User, error)
}

type APIKeyRepository interface {
	Create(ctx context.Context, userID, label, apiKey string, encryptedSecret []byte) (*domain.APIKey, error)
	GetByID(ctx context.Context, id string) (*domain.APIKey, []byte, error)
	GetByUserID(ctx context.Context, userID string) (*domain.APIKey, []byte, error)
	Delete(ctx context.Context, id, userID string) error
}

type ConfigRepository interface {
	Upsert(ctx context.Context, userID string, configJSON []byte) (*domain.UserConfig, error)
	GetByUserID(ctx context.Context, userID string) (*domain.UserConfig, error)
	DeleteByUserID(ctx context.Context, userID string) error
}
