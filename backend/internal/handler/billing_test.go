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

func (m *testBillingRepo) ListByUserPaginated(_ context.Context, userID string, cursorTime *time.Time, cursorID string, limit int) ([]domain.BillingRecord, error) {
	var result []domain.BillingRecord
	for _, r := range m.records {
		if r.UserID != userID {
			continue
		}
		if cursorTime != nil {
			if r.CreatedAt.After(*cursorTime) || (r.CreatedAt.Equal(*cursorTime) && r.ID >= cursorID) {
				continue
			}
		}
		result = append(result, r)
		if len(result) >= limit {
			break
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
func (m *testBillingUserRepo) UpdateStatus(_ context.Context, _ string, _ domain.UserStatus) error {
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
			{ID: "1", UserID: "user-123", Plan: domain.PlanStarter, Amount: 9.99, Status: domain.BillingStatusPaid, PeriodStart: now.AddDate(0, -1, 0), CreatedAt: now},
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

	var body struct {
		Data       []domain.BillingRecord `json:"data"`
		Pagination PaginationResponse     `json:"pagination"`
	}
	json.NewDecoder(w.Body).Decode(&body)
	if len(body.Data) != 1 {
		t.Errorf("expected 1 record, got %d", len(body.Data))
	}
	if body.Pagination.HasMore {
		t.Error("expected has_more=false for single record")
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

	var envelope struct {
		Data domain.PlanFeatures `json:"data"`
	}
	json.NewDecoder(w.Body).Decode(&envelope)
	if envelope.Data.Plan != domain.PlanPro {
		t.Errorf("expected pro, got %s", envelope.Data.Plan)
	}
	if !envelope.Data.AdvancedStrategy {
		t.Error("expected advancedStrategy=true for pro")
	}
}

func TestBillingHandler_Get_InvalidCursor(t *testing.T) {
	billingRepo := &testBillingRepo{}
	userRepo := &testBillingUserRepo{users: map[string]*domain.User{}}
	r := setupBillingRouter(billingRepo, userRepo)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/billing?after=!!!bad!!!", nil)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", w.Code)
	}
}
