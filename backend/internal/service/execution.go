package service

import (
	"context"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/repository"
)

type ExecutionService struct {
	repo repository.ExecutionRepository
}

func NewExecutionService(repo repository.ExecutionRepository) *ExecutionService {
	return &ExecutionService{repo: repo}
}

func (s *ExecutionService) ListByUser(ctx context.Context, userID string, since time.Time, limit int) ([]domain.ExecutionRecord, error) {
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	if since.IsZero() {
		since = time.Now().AddDate(0, 0, -7)
	}
	return s.repo.ListByUser(ctx, userID, since, limit)
}
