package service

import (
	"context"
	"encoding/json"

	"go.uber.org/zap"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/repository"
)

// ConfigReloader notifies a Worker to hot-reload new strategy config.
// Satisfied by *lending.Service via implicit interface (fx auto-inject).
type ConfigReloader interface {
	ReloadConfig(ctx context.Context, userID string, cfg domain.StrategyConfig) error
}

type ConfigService struct {
	repo     repository.ConfigRepository
	reloader ConfigReloader
	log      *zap.Logger
}

func NewConfigService(repo repository.ConfigRepository, reloader ConfigReloader, log *zap.Logger) *ConfigService {
	return &ConfigService{repo: repo, reloader: reloader, log: log}
}

func (s *ConfigService) Save(ctx context.Context, userID string, cfg domain.StrategyConfig) (*domain.UserConfig, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}

	configJSON, err := json.Marshal(cfg)
	if err != nil {
		return nil, domain.ErrInternal("failed to serialize config")
	}

	uc, err := s.repo.Upsert(ctx, userID, configJSON)
	if err != nil {
		return nil, domain.ErrInternal("failed to save config")
	}

	// Best-effort: notify Worker to hot-reload
	if s.reloader != nil {
		if err := s.reloader.ReloadConfig(ctx, userID, cfg); err != nil {
			s.log.Warn("failed to reload worker config",
				zap.String("userID", userID), zap.Error(err))
		}
	}

	return uc, nil
}

func (s *ConfigService) Get(ctx context.Context, userID string) (*domain.UserConfig, error) {
	uc, err := s.repo.GetByUserID(ctx, userID)
	if err != nil {
		return nil, domain.ErrInternal("failed to query config")
	}
	if uc == nil {
		return nil, domain.ErrConfigNotFound()
	}
	return uc, nil
}

func (s *ConfigService) Delete(ctx context.Context, userID string) error {
	return s.repo.DeleteByUserID(ctx, userID)
}
