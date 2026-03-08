package service

import (
	"context"
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

type mockBillingRepo struct {
	records []domain.BillingRecord
}

func (m *mockBillingRepo) Create(ctx context.Context, record *domain.BillingRecord) (*domain.BillingRecord, error) {
	m.records = append(m.records, *record)
	return record, nil
}

func (m *mockBillingRepo) ListByUser(ctx context.Context, userID string, since time.Time, limit int) ([]domain.BillingRecord, error) {
	var result []domain.BillingRecord
	for _, r := range m.records {
		if r.UserID == userID && !r.PeriodStart.Before(since) {
			result = append(result, r)
			if len(result) >= limit {
				break
			}
		}
	}
	return result, nil
}

func TestBillingService_GetBilling_Summary(t *testing.T) {
	now := time.Now()
	repo := &mockBillingRepo{
		records: []domain.BillingRecord{
			{ID: "1", UserID: "u1", Plan: domain.PlanStarter, Amount: 9.99, Status: domain.BillingStatusPaid, PeriodStart: now.AddDate(0, -1, 0)},
			{ID: "2", UserID: "u1", Plan: domain.PlanStarter, Amount: 9.99, Status: domain.BillingStatusPending, PeriodStart: now},
		},
	}
	userRepo := newMockRepo()
	userRepo.users["u1"] = &domain.User{ID: "u1", Plan: domain.PlanStarter}

	svc := NewBillingService(repo, userRepo)
	summary, err := svc.GetBilling(context.Background(), "u1", time.Time{}, 0)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(summary.Records) != 2 {
		t.Errorf("expected 2 records, got %d", len(summary.Records))
	}
	if summary.TotalPaid != 9.99 {
		t.Errorf("expected total_paid=9.99, got %f", summary.TotalPaid)
	}
	if summary.TotalPending != 9.99 {
		t.Errorf("expected total_pending=9.99, got %f", summary.TotalPending)
	}
}

func TestBillingService_GetBilling_Empty(t *testing.T) {
	repo := &mockBillingRepo{}
	userRepo := newMockRepo()

	svc := NewBillingService(repo, userRepo)
	summary, err := svc.GetBilling(context.Background(), "u1", time.Time{}, 0)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(summary.Records) != 0 {
		t.Errorf("expected empty records, got %d", len(summary.Records))
	}
}

func TestBillingService_GetPlan(t *testing.T) {
	repo := &mockBillingRepo{}
	userRepo := newMockRepo()
	userRepo.users["u1"] = &domain.User{ID: "u1", Plan: domain.PlanPro}

	svc := NewBillingService(repo, userRepo)
	features, err := svc.GetPlan(context.Background(), "u1")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if features.Plan != domain.PlanPro {
		t.Errorf("expected pro plan, got %s", features.Plan)
	}
	if !features.AdvancedStrategy {
		t.Error("expected advanced_strategy=true for pro plan")
	}
	if features.CustomParams {
		t.Error("expected custom_params=false for pro plan")
	}
}
