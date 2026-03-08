package repository

import (
	"context"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

type UserRepository interface {
	Create(ctx context.Context, email, passwordHash string) (*domain.User, error)
	GetByID(ctx context.Context, id string) (*domain.User, error)
	GetByEmail(ctx context.Context, email string) (*domain.User, error)
	UpdatePassword(ctx context.Context, id, passwordHash string) error
}

type APIKeyRepository interface {
	Create(ctx context.Context, userID, label, apiKey string, encryptedSecret []byte, exchangeStatus string) (*domain.APIKey, error)
	GetByID(ctx context.Context, id string) (*domain.APIKey, []byte, error)
	GetByUserID(ctx context.Context, userID string) (*domain.APIKey, []byte, error)
	ListVerified(ctx context.Context) ([]domain.APIKey, [][]byte, error)
	UpdateExchangeStatus(ctx context.Context, id, status string) error
	Delete(ctx context.Context, id, userID string) error
}

type ConfigRepository interface {
	Upsert(ctx context.Context, userID string, configJSON []byte) (*domain.UserConfig, error)
	GetByUserID(ctx context.Context, userID string) (*domain.UserConfig, error)
	DeleteByUserID(ctx context.Context, userID string) error
}

type ExecutionRepository interface {
	Create(ctx context.Context, record *domain.ExecutionRecord) (*domain.ExecutionRecord, error)
	ListByUser(ctx context.Context, userID string, since time.Time, limit int) ([]domain.ExecutionRecord, error)
}
