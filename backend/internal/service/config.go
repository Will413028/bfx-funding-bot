package service

import (
	"context"
	"encoding/json"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/repository"
)

type ConfigService struct {
	repo repository.ConfigRepository
}

func NewConfigService(repo repository.ConfigRepository) *ConfigService {
	return &ConfigService{repo: repo}
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
