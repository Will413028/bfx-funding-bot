package handler

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

type mockEngineHealth struct {
	status domain.EngineStatus
}

func (m *mockEngineHealth) Status() domain.EngineStatus {
	return m.status
}

func TestHealthHandler_EngineRunning(t *testing.T) {
	gin.SetMode(gin.TestMode)

	engine := &mockEngineHealth{
		status: domain.EngineStatus{
			Running:     true,
			WorkerCount: 3,
		},
	}

	h := &HealthHandler{engine: engine}

	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(http.MethodGet, "/health", nil)

	h.Status(c)

	// Should not fail on DB/Redis ping — those are nil in this test,
	// so we only check the engine portion of the response
	var resp map[string]any
	json.Unmarshal(w.Body.Bytes(), &resp)

	engineData, ok := resp["engine"].(map[string]any)
	if !ok {
		t.Fatal("expected engine field in response")
	}
	if engineData["running"] != true {
		t.Errorf("expected running=true, got %v", engineData["running"])
	}
	if engineData["worker_count"] != float64(3) {
		t.Errorf("expected worker_count=3, got %v", engineData["worker_count"])
	}
}

func TestHealthHandler_EngineStopped(t *testing.T) {
	gin.SetMode(gin.TestMode)

	engine := &mockEngineHealth{
		status: domain.EngineStatus{
			Running:     false,
			WorkerCount: 0,
		},
	}

	h := &HealthHandler{engine: engine}

	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(http.MethodGet, "/health", nil)

	h.Status(c)

	var resp map[string]any
	json.Unmarshal(w.Body.Bytes(), &resp)

	engineData, ok := resp["engine"].(map[string]any)
	if !ok {
		t.Fatal("expected engine field in response")
	}
	if engineData["running"] != false {
		t.Errorf("expected running=false, got %v", engineData["running"])
	}
}

func TestHealthHandler_NilEngine(t *testing.T) {
	gin.SetMode(gin.TestMode)

	h := &HealthHandler{engine: nil}

	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(http.MethodGet, "/health", nil)

	h.Status(c)

	var resp map[string]any
	json.Unmarshal(w.Body.Bytes(), &resp)

	if _, ok := resp["engine"]; ok {
		t.Error("expected no engine field when engine is nil")
	}
}
