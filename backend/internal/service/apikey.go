package service

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5/pgconn"

	"github.com/will/bfx-funding-bot/backend/internal/bitfinex"
	"github.com/will/bfx-funding-bot/backend/internal/crypto"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/repository"
)

type VerifyResult struct {
	Status         string
	FundingBalance *domain.Wallet
	Error          string
}

type APIKeyService struct {
	repo   repository.APIKeyRepository
	cipher *crypto.AES
	bfx    *bitfinex.Client
}

func NewAPIKeyService(repo repository.APIKeyRepository, cipher *crypto.AES, bfx *bitfinex.Client) *APIKeyService {
	return &APIKeyService{repo: repo, cipher: cipher, bfx: bfx}
}

func (s *APIKeyService) Create(ctx context.Context, userID, apiKey, apiSecret, label string) (*domain.APIKey, *VerifyResult, error) {
	encrypted, err := s.cipher.Encrypt([]byte(apiSecret))
	if err != nil {
		return nil, nil, domain.ErrInternal("failed to encrypt API secret")
	}

	// Verify credentials against Bitfinex API
	vr := s.verifyKey(ctx, apiKey, apiSecret)

	key, err := s.repo.Create(ctx, userID, label, apiKey, encrypted, vr.Status)
	if err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" {
			return nil, nil, domain.ErrAPIKeyAlreadyExists()
		}
		return nil, nil, domain.ErrInternal("failed to create API key")
	}

	return key, vr, nil
}

func (s *APIKeyService) Verify(ctx context.Context, userID, keyID string) (*VerifyResult, error) {
	key, encryptedSecret, err := s.repo.GetByID(ctx, keyID)
	if err != nil {
		return nil, domain.ErrAPIKeyNotFound()
	}
	if key.UserID != userID {
		return nil, domain.ErrAPIKeyNotFound()
	}

	secret, err := s.cipher.Decrypt(encryptedSecret)
	if err != nil {
		return nil, domain.ErrInternal("failed to decrypt API secret")
	}

	vr := s.verifyKey(ctx, key.APIKey, string(secret))

	if err := s.repo.UpdateExchangeStatus(ctx, keyID, vr.Status); err != nil {
		return nil, domain.ErrInternal("failed to update exchange status")
	}

	return vr, nil
}

func (s *APIKeyService) verifyKey(ctx context.Context, apiKey, apiSecret string) *VerifyResult {
	err := s.bfx.VerifyCredentials(ctx, apiKey, apiSecret)
	if err != nil {
		return &VerifyResult{
			Status: "unverified",
			Error:  err.Error(),
		}
	}

	wallet, err := s.bfx.GetFundingBalance(ctx, apiKey, apiSecret, "USD")
	if err != nil {
		return &VerifyResult{
			Status: "verified",
		}
	}

	return &VerifyResult{
		Status:         "verified",
		FundingBalance: wallet,
	}
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
