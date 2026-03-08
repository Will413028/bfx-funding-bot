package handler

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/middleware"
	"github.com/will/bfx-funding-bot/backend/internal/service"
)

type testBillingRepo struct {
	records []domain.BillingRecord
}

func (m *testBillingRepo) Create(_ context.Context, record *domain.BillingRecord) (*domain.BillingRecord, error) {
	m.records = append(m.records, *record)
	return record, nil
}

func (m *testBillingRepo) ListByUser(_ context.Context, userID string, since time.Time, limit int) ([]domain.BillingRecord, error) {
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

type testBillingUserRepo struct {
	users map[string]*domain.User
}

func (m *testBillingUserRepo) Create(_ context.Context, email, passwordHash string) (*domain.User, error) {
	return nil, nil
}
func (m *testBillingUserRepo) GetByID(_ context.Context, id string) (*domain.User, error) {
	u, ok := m.users[id]
	if !ok {
		return nil, pgx.ErrNoRows
	}
	return u, nil
}
func (m *testBillingUserRepo) GetByEmail(_ context.Context, email string) (*domain.User, error) {
	return nil, nil
}
func (m *testBillingUserRepo) UpdatePassword(_ context.Context, id, passwordHash string) error {
	return nil
}

func setupBillingRouter(billingRepo *testBillingRepo, userRepo *testBillingUserRepo) *gin.Engine {
	gin.SetMode(gin.TestMode)
	svc := service.NewBillingService(billingRepo, userRepo)
	h := NewBillingHandler(svc)

	r := gin.New()
	r.GET("/billing", func(c *gin.Context) {
		c.Set(middleware.ContextUserID, "user-123")
		h.Get(c)
	})
	r.GET("/billing/plan", func(c *gin.Context) {
		c.Set(middleware.ContextUserID, "user-123")
		h.GetPlan(c)
	})
	return r
}

func TestBillingHandler_Get(t *testing.T) {
	now := time.Now()
	billingRepo := &testBillingRepo{
		records: []domain.BillingRecord{
			{ID: "1", UserID: "user-123", Plan: domain.PlanStarter, Amount: 9.99, Status: domain.BillingStatusPaid, PeriodStart: now.AddDate(0, -1, 0)},
		},
	}
	userRepo := &testBillingUserRepo{users: map[string]*domain.User{
		"user-123": {ID: "user-123", Plan: domain.PlanStarter},
	}}
	r := setupBillingRouter(billingRepo, userRepo)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/billing", nil)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", w.Code)
	}

	var body service.BillingSummary
	json.NewDecoder(w.Body).Decode(&body)
	if len(body.Records) != 1 {
		t.Errorf("expected 1 record, got %d", len(body.Records))
	}
	if body.TotalPaid != 9.99 {
		t.Errorf("expected total_paid=9.99, got %f", body.TotalPaid)
	}
}

func TestBillingHandler_GetPlan(t *testing.T) {
	billingRepo := &testBillingRepo{}
	userRepo := &testBillingUserRepo{users: map[string]*domain.User{
		"user-123": {ID: "user-123", Plan: domain.PlanPro},
	}}
	r := setupBillingRouter(billingRepo, userRepo)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/billing/plan", nil)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", w.Code)
	}

	var features domain.PlanFeatures
	json.NewDecoder(w.Body).Decode(&features)
	if features.Plan != domain.PlanPro {
		t.Errorf("expected pro, got %s", features.Plan)
	}
	if !features.AdvancedStrategy {
		t.Error("expected advanced_strategy=true for pro")
	}
}

func TestBillingHandler_Get_InvalidSince(t *testing.T) {
	billingRepo := &testBillingRepo{}
	userRepo := &testBillingUserRepo{users: map[string]*domain.User{}}
	r := setupBillingRouter(billingRepo, userRepo)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/billing?since=bad", nil)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", w.Code)
	}
}
