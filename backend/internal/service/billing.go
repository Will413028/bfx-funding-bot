package service

import (
	"context"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/repository"
)

type BillingSummary struct {
	Records      []domain.BillingRecord `json:"records"`
	TotalPaid    float64                `json:"totalPaid"`
	TotalPending float64                `json:"totalPending"`
}

type BillingService struct {
	repo     repository.BillingRepository
	userRepo repository.UserRepository
}

func NewBillingService(repo repository.BillingRepository, userRepo repository.UserRepository) *BillingService {
	return &BillingService{repo: repo, userRepo: userRepo}
}

func (s *BillingService) GetBilling(ctx context.Context, userID string, since time.Time, limit int) (*BillingSummary, error) {
	if limit <= 0 || limit > 100 {
		limit = 12
	}
	if since.IsZero() {
		since = time.Now().AddDate(-1, 0, 0)
	}

	records, err := s.repo.ListByUser(ctx, userID, since, limit)
	if err != nil {
		return nil, err
	}

	summary := &BillingSummary{Records: records}
	for _, r := range records {
		switch r.Status {
		case domain.BillingStatusPaid:
			summary.TotalPaid += r.Amount
		case domain.BillingStatusPending, domain.BillingStatusOverdue:
			summary.TotalPending += r.Amount
		}
	}

	if summary.Records == nil {
		summary.Records = []domain.BillingRecord{}
	}

	return summary, nil
}

func (s *BillingService) ListByUserPaginated(ctx context.Context, userID string, cursorTime *time.Time, cursorID string, limit int) ([]domain.BillingRecord, error) {
	return s.repo.ListByUserPaginated(ctx, userID, cursorTime, cursorID, limit)
}

func (s *BillingService) GetPlan(ctx context.Context, userID string) (*domain.PlanFeatures, error) {
	user, err := s.userRepo.GetByID(ctx, userID)
	if err != nil {
		return nil, err
	}
	features := domain.GetPlanFeatures(user.Plan)
	return &features, nil
}
