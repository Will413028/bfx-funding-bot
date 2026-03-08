package handler

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/middleware"
	"github.com/will/bfx-funding-bot/backend/internal/service"
)

type testExecutionRepo struct {
	records []domain.ExecutionRecord
}

func (m *testExecutionRepo) Create(_ context.Context, record *domain.ExecutionRecord) (*domain.ExecutionRecord, error) {
	m.records = append(m.records, *record)
	return record, nil
}

func (m *testExecutionRepo) ListByUserPaginated(_ context.Context, userID string, cursorTime *time.Time, cursorID string, limit int) ([]domain.ExecutionRecord, error) {
	return m.ListByUser(context.Background(), userID, time.Time{}, limit)
}

func (m *testExecutionRepo) ListByUser(_ context.Context, userID string, since time.Time, limit int) ([]domain.ExecutionRecord, error) {
	var result []domain.ExecutionRecord
	for _, r := range m.records {
		if r.UserID == userID && !r.CreatedAt.Before(since) {
			result = append(result, r)
			if len(result) >= limit {
				break
			}
		}
	}
	return result, nil
}

func setupExecutionRouter(repo *testExecutionRepo) *gin.Engine {
	gin.SetMode(gin.TestMode)
	svc := service.NewExecutionService(repo)
	h := NewExecutionHandler(svc)

	r := gin.New()
	r.GET("/executions", func(c *gin.Context) {
		c.Set(middleware.ContextUserID, "user-123")
		h.List(c)
	})
	return r
}

func TestExecutionHandler_List_Default(t *testing.T) {
	now := time.Now()
	repo := &testExecutionRepo{
		records: []domain.ExecutionRecord{
			{ID: "1", UserID: "user-123", Action: domain.ActionPlace, Currency: "fUSD", Amount: 500, Rate: 0.0005, Period: 2, Status: "success", CreatedAt: now},
		},
	}
	r := setupExecutionRouter(repo)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/executions", nil)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", w.Code)
	}

	var body map[string][]domain.ExecutionRecord
	json.NewDecoder(w.Body).Decode(&body)
	if len(body["executions"]) != 1 {
		t.Errorf("expected 1 execution, got %d", len(body["executions"]))
	}
}

func TestExecutionHandler_List_WithSince(t *testing.T) {
	now := time.Now()
	old := now.AddDate(0, 0, -10)
	repo := &testExecutionRepo{
		records: []domain.ExecutionRecord{
			{ID: "1", UserID: "user-123", Action: domain.ActionPlace, Currency: "fUSD", Amount: 100, Rate: 0.001, Period: 2, Status: "success", CreatedAt: old},
			{ID: "2", UserID: "user-123", Action: domain.ActionFilled, Currency: "fUSD", Amount: 100, Rate: 0.001, Period: 2, Status: "success", CreatedAt: now},
		},
	}
	r := setupExecutionRouter(repo)

	since := now.AddDate(0, 0, -3).UTC().Format(time.RFC3339)
	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/executions?since="+since, nil)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", w.Code)
	}

	var body map[string][]domain.ExecutionRecord
	json.NewDecoder(w.Body).Decode(&body)
	if len(body["executions"]) != 1 {
		t.Errorf("expected 1 recent execution, got %d", len(body["executions"]))
	}
}

func TestExecutionHandler_List_InvalidSince(t *testing.T) {
	repo := &testExecutionRepo{}
	r := setupExecutionRouter(repo)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/executions?since=not-a-date", nil)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", w.Code)
	}
}

func TestExecutionHandler_List_InvalidLimit(t *testing.T) {
	repo := &testExecutionRepo{}
	r := setupExecutionRouter(repo)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/executions?limit=abc", nil)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", w.Code)
	}
}
