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
	ListByUserPaginated(ctx context.Context, userID string, cursorTime *time.Time, cursorID string, limit int) ([]domain.ExecutionRecord, error)
}

type BillingRepository interface {
	Create(ctx context.Context, record *domain.BillingRecord) (*domain.BillingRecord, error)
	ListByUser(ctx context.Context, userID string, since time.Time, limit int) ([]domain.BillingRecord, error)
	ListByUserPaginated(ctx context.Context, userID string, cursorTime *time.Time, cursorID string, limit int) ([]domain.BillingRecord, error)
}

type SnapshotCache interface {
	Set(ctx context.Context, symbol string, snapshot *domain.MarketSnapshot, ttl time.Duration) error
	Get(ctx context.Context, symbol string) (*domain.MarketSnapshot, error)
	Delete(ctx context.Context, symbol string) error
}

type SnapshotPubSub interface {
	Publish(ctx context.Context, snapshot *domain.MarketSnapshot) error
	Subscribe(ctx context.Context) (<-chan *domain.MarketSnapshot, error)
	Close() error
}
