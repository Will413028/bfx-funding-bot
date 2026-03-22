package handler

import (
	"bytes"
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
	"go.uber.org/zap"
)

type testConfigRepo struct {
	configs map[string]*domain.UserConfig
}

func (m *testConfigRepo) Upsert(_ context.Context, userID string, configJSON []byte) (*domain.UserConfig, error) {
	var cfg domain.StrategyConfig
	if err := json.Unmarshal(configJSON, &cfg); err != nil {
		return nil, err
	}
	now := time.Now()
	if uc, ok := m.configs[userID]; ok {
		uc.Config = cfg
		uc.UpdatedAt = now
		return uc, nil
	}
	uc := &domain.UserConfig{
		ID:        "cfg-" + userID,
		UserID:    userID,
		Config:    cfg,
		CreatedAt: now,
		UpdatedAt: now,
	}
	m.configs[userID] = uc
	return uc, nil
}

func (m *testConfigRepo) GetByUserID(_ context.Context, userID string) (*domain.UserConfig, error) {
	uc, ok := m.configs[userID]
	if !ok {
		return nil, nil
	}
	return uc, nil
}

func (m *testConfigRepo) DeleteByUserID(_ context.Context, userID string) error {
	if _, ok := m.configs[userID]; !ok {
		return domain.ErrConfigNotFound()
	}
	delete(m.configs, userID)
	return nil
}

func setupConfigRouter(t *testing.T) *gin.Engine {
	t.Helper()

	repo := &testConfigRepo{configs: make(map[string]*domain.UserConfig)}
	svc := service.NewConfigService(repo, nil, zap.NewNop())
	h := NewConfigHandler(svc)

	r := gin.New()
	r.Use(func(c *gin.Context) {
		c.Set(middleware.ContextUserID, "test-user-id")
		c.Next()
	})
	r.PUT("/configs", h.Save)
	r.GET("/configs", h.Get)
	r.DELETE("/configs", h.Delete)

	return r
}

func validConfigJSON() []byte {
	body, _ := json.Marshal(map[string]interface{}{
		"currency":  "USD",
		"amount":    map[string]float64{"min": 50, "max": 1000},
		"rate":      map[string]float64{"min": 0.0001, "max": 0.001},
		"period":    map[string]int{"min": 2, "max": 30},
		"autoRenew": true,
	})
	return body
}

func TestHandler_SaveConfig_Success(t *testing.T) {
	r := setupConfigRouter(t)

	req := httptest.NewRequest(http.MethodPut, "/configs", bytes.NewReader(validConfigJSON()))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w.Code, w.Body.String())
	}

	var envelope map[string]interface{}
	_ = json.Unmarshal(w.Body.Bytes(), &envelope)
	resp := envelope["data"].(map[string]interface{})
	if resp["id"] == nil {
		t.Error("expected id in response")
	}
	cfg, ok := resp["config"].(map[string]interface{})
	if !ok {
		t.Fatal("expected config object in response")
	}
	if cfg["currency"] != "USD" {
		t.Errorf("expected currency USD, got %v", cfg["currency"])
	}
}

func TestHandler_SaveConfig_ValidationError(t *testing.T) {
	r := setupConfigRouter(t)

	body, _ := json.Marshal(map[string]interface{}{
		"currency": "",
		"amount":   map[string]float64{"min": 10, "max": 5},
		"rate":     map[string]float64{"min": 0, "max": 0},
		"period":   map[string]int{"min": 1, "max": 200},
	})

	req := httptest.NewRequest(http.MethodPut, "/configs", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d: %s", w.Code, w.Body.String())
	}
}

func TestHandler_GetConfig_Found(t *testing.T) {
	r := setupConfigRouter(t)

	// First save
	req1 := httptest.NewRequest(http.MethodPut, "/configs", bytes.NewReader(validConfigJSON()))
	req1.Header.Set("Content-Type", "application/json")
	w1 := httptest.NewRecorder()
	r.ServeHTTP(w1, req1)

	// Then get
	req2 := httptest.NewRequest(http.MethodGet, "/configs", nil)
	w2 := httptest.NewRecorder()
	r.ServeHTTP(w2, req2)

	if w2.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w2.Code, w2.Body.String())
	}
}

func TestHandler_GetConfig_NotFound(t *testing.T) {
	r := setupConfigRouter(t)

	req := httptest.NewRequest(http.MethodGet, "/configs", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d: %s", w.Code, w.Body.String())
	}
}

func TestHandler_DeleteConfig_Success(t *testing.T) {
	r := setupConfigRouter(t)

	// First save
	req1 := httptest.NewRequest(http.MethodPut, "/configs", bytes.NewReader(validConfigJSON()))
	req1.Header.Set("Content-Type", "application/json")
	w1 := httptest.NewRecorder()
	r.ServeHTTP(w1, req1)

	// Then delete
	req2 := httptest.NewRequest(http.MethodDelete, "/configs", nil)
	w2 := httptest.NewRecorder()
	r.ServeHTTP(w2, req2)

	if w2.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d: %s", w2.Code, w2.Body.String())
	}
}

func TestHandler_DeleteConfig_NotFound(t *testing.T) {
	r := setupConfigRouter(t)

	req := httptest.NewRequest(http.MethodDelete, "/configs", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d: %s", w.Code, w.Body.String())
	}
}
