package service

import (
	"context"
	"encoding/json"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/repository"
)

// ConfigReloader notifies a Worker to hot-reload new strategy config.
// Satisfied by *lending.Service via implicit interface (fx auto-inject).
type ConfigReloader interface {
	ReloadConfig(userID string, cfg domain.StrategyConfig) error
}

type ConfigService struct {
	repo     repository.ConfigRepository
	reloader ConfigReloader
}

func NewConfigService(repo repository.ConfigRepository, reloader ConfigReloader) *ConfigService {
	return &ConfigService{repo: repo, reloader: reloader}
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
		_ = s.reloader.ReloadConfig(userID, cfg)
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
