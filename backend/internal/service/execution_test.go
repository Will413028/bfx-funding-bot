package service

import (
	"context"
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

type mockExecutionRepo struct {
	records []domain.ExecutionRecord
}

func (m *mockExecutionRepo) Create(ctx context.Context, record *domain.ExecutionRecord) (*domain.ExecutionRecord, error) {
	m.records = append(m.records, *record)
	return record, nil
}

func (m *mockExecutionRepo) ListByUserPaginated(ctx context.Context, userID string, cursorTime *time.Time, cursorID string, limit int) ([]domain.ExecutionRecord, error) {
	return m.ListByUser(ctx, userID, time.Time{}, limit)
}

func (m *mockExecutionRepo) ListByUser(ctx context.Context, userID string, since time.Time, limit int) ([]domain.ExecutionRecord, error) {
	var result []domain.ExecutionRecord
	for i := range m.records {
		if m.records[i].UserID == userID && !m.records[i].CreatedAt.Before(since) {
			result = append(result, m.records[i])
			if len(result) >= limit {
				break
			}
		}
	}
	return result, nil
}

func TestExecutionService_ListByUser_DefaultLimit(t *testing.T) {
	now := time.Now()
	repo := &mockExecutionRepo{
		records: []domain.ExecutionRecord{
			{ID: "1", UserID: "u1", Action: domain.ActionPlace, Currency: "fUSD", Amount: 100, Rate: 0.001, Period: 2, Status: "success", CreatedAt: now},
			{ID: "2", UserID: "u1", Action: domain.ActionFilled, Currency: "fUSD", Amount: 100, Rate: 0.001, Period: 2, Status: "success", CreatedAt: now},
		},
	}
	svc := NewExecutionService(repo)

	records, err := svc.ListByUser(context.Background(), "u1", time.Time{}, 0)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(records) != 2 {
		t.Errorf("expected 2 records, got %d", len(records))
	}
}

func TestExecutionService_ListByUser_CapsLimit(t *testing.T) {
	repo := &mockExecutionRepo{}
	svc := NewExecutionService(repo)

	// limit > 200 should be capped to 50
	_, err := svc.ListByUser(context.Background(), "u1", time.Now().Add(-24*time.Hour), 999)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestExecutionService_ListByUser_WithParams(t *testing.T) {
	now := time.Now()
	old := now.AddDate(0, 0, -10)
	repo := &mockExecutionRepo{
		records: []domain.ExecutionRecord{
			{ID: "1", UserID: "u1", Action: domain.ActionPlace, Currency: "fUSD", Amount: 100, Rate: 0.001, Period: 2, Status: "success", CreatedAt: old},
			{ID: "2", UserID: "u1", Action: domain.ActionCancel, Currency: "fUSD", Amount: 100, Rate: 0.001, Period: 2, Status: "success", CreatedAt: now},
		},
	}
	svc := NewExecutionService(repo)

	since := now.AddDate(0, 0, -3)
	records, err := svc.ListByUser(context.Background(), "u1", since, 50)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(records) != 1 {
		t.Errorf("expected 1 recent record, got %d", len(records))
	}
}
