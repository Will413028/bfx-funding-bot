package service

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5/pgconn"

	"github.com/will/bfx-funding-bot/backend/internal/crypto"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/repository"
)

type APIKeyService struct {
	repo   repository.APIKeyRepository
	cipher *crypto.AES
}

func NewAPIKeyService(repo repository.APIKeyRepository, cipher *crypto.AES) *APIKeyService {
	return &APIKeyService{repo: repo, cipher: cipher}
}

func (s *APIKeyService) Create(ctx context.Context, userID, apiKey, apiSecret, label string) (*domain.APIKey, error) {
	encrypted, err := s.cipher.Encrypt([]byte(apiSecret))
	if err != nil {
		return nil, domain.ErrInternal("failed to encrypt API secret")
	}

	key, err := s.repo.Create(ctx, userID, label, apiKey, encrypted)
	if err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" {
			return nil, domain.ErrAPIKeyAlreadyExists()
		}
		return nil, domain.ErrInternal("failed to create API key")
	}

	return key, nil
}

func (s *APIKeyService) List(ctx context.Context, userID string) ([]domain.APIKey, error) {
	key, _, err := s.repo.GetByUserID(ctx, userID)
	if err != nil {
		return nil, domain.ErrInternal("failed to query API keys")
	}
	if key == nil {
		return []domain.APIKey{}, nil
	}
	return []domain.APIKey{*key}, nil
}

func (s *APIKeyService) GetByID(ctx context.Context, userID, keyID string) (*domain.APIKey, error) {
	key, _, err := s.repo.GetByID(ctx, keyID)
	if err != nil {
		return nil, domain.ErrAPIKeyNotFound()
	}
	if key.UserID != userID {
		return nil, domain.ErrAPIKeyNotFound()
	}
	return key, nil
}

func (s *APIKeyService) Delete(ctx context.Context, userID, keyID string) error {
	return s.repo.Delete(ctx, keyID, userID)
}
