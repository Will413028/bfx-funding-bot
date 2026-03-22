package service

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5/pgconn"
	"go.uber.org/zap"

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

// WorkerManager manages per-user Worker lifecycle.
// Satisfied by *lending.Service via implicit interface (fx auto-inject).
type WorkerManager interface {
	StartWorker(ctx context.Context, userID string, cfg domain.StrategyConfig) error
	StopWorker(ctx context.Context, userID string) error
}

type APIKeyService struct {
	repo       repository.APIKeyRepository
	configRepo repository.ConfigRepository
	cipher     *crypto.AES
	bfx        *bitfinex.Client
	workers    WorkerManager
	log        *zap.Logger
}

func NewAPIKeyService(
	repo repository.APIKeyRepository,
	configRepo repository.ConfigRepository,
	cipher *crypto.AES,
	bfx *bitfinex.Client,
	workers WorkerManager,
	log *zap.Logger,
) *APIKeyService {
	return &APIKeyService{
		repo:       repo,
		configRepo: configRepo,
		cipher:     cipher,
		bfx:        bfx,
		workers:    workers,
		log:        log,
	}
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

	// Best-effort: start Worker if key is verified and user has a strategy config
	if vr.Status == "verified" && s.workers != nil {
		s.tryStartWorker(ctx, userID)
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
	err := s.repo.Delete(ctx, keyID, userID)
	if err != nil {
		return err
	}

	// Best-effort: stop Worker
	if s.workers != nil {
		_ = s.workers.StopWorker(ctx, userID)
	}

	return nil
}

// tryStartWorker looks up the user's strategy config and starts a Worker.
// Errors are silently ignored (best-effort).
func (s *APIKeyService) tryStartWorker(ctx context.Context, userID string) {
	uc, err := s.configRepo.GetByUserID(ctx, userID)
	if err != nil || uc == nil {
		return
	}

	if err := s.workers.StartWorker(ctx, userID, uc.Config); err != nil {
		s.log.Warn("failed to start worker after API key verification",
			zap.String("userID", userID), zap.Error(err))
	}
}
