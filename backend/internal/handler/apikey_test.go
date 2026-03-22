package handler

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/will/bfx-funding-bot/backend/internal/bitfinex"
	"github.com/will/bfx-funding-bot/backend/internal/crypto"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/middleware"
	"github.com/will/bfx-funding-bot/backend/internal/service"
	"go.uber.org/zap"
)

func init() {
	gin.SetMode(gin.TestMode)
}

type testAPIKeyRepo struct {
	keys map[string]*testStoredKey
}

type testStoredKey struct {
	domainKey       *domain.APIKey
	encryptedSecret []byte
}

func (m *testAPIKeyRepo) Create(_ context.Context, userID, label, apiKey string, encryptedSecret []byte, exchangeStatus string) (*domain.APIKey, error) {
	for _, sk := range m.keys {
		if sk.domainKey.UserID == userID {
			return nil, &pgconn.PgError{Code: "23505"}
		}
	}
	k := &domain.APIKey{
		ID:             "key-" + userID,
		UserID:         userID,
		Label:          label,
		APIKey:         apiKey,
		ExchangeStatus: exchangeStatus,
		CreatedAt:      time.Now(),
		UpdatedAt:      time.Now(),
	}
	m.keys[k.ID] = &testStoredKey{domainKey: k, encryptedSecret: encryptedSecret}
	return k, nil
}

func (m *testAPIKeyRepo) GetByID(_ context.Context, id string) (*domain.APIKey, []byte, error) {
	sk, ok := m.keys[id]
	if !ok {
		return nil, nil, domain.ErrAPIKeyNotFound()
	}
	return sk.domainKey, sk.encryptedSecret, nil
}

func (m *testAPIKeyRepo) GetByUserID(_ context.Context, userID string) (*domain.APIKey, []byte, error) {
	for _, sk := range m.keys {
		if sk.domainKey.UserID == userID {
			return sk.domainKey, sk.encryptedSecret, nil
		}
	}
	return nil, nil, nil
}

func (m *testAPIKeyRepo) UpdateExchangeStatus(_ context.Context, id, status string) error {
	sk, ok := m.keys[id]
	if !ok {
		return domain.ErrAPIKeyNotFound()
	}
	sk.domainKey.ExchangeStatus = status
	return nil
}

func (m *testAPIKeyRepo) Delete(_ context.Context, id, userID string) error {
	sk, ok := m.keys[id]
	if !ok || sk.domainKey.UserID != userID {
		return domain.ErrAPIKeyNotFound()
	}
	delete(m.keys, id)
	return nil
}

func (m *testAPIKeyRepo) ListVerified(_ context.Context) ([]domain.APIKey, [][]byte, error) {
	return nil, nil, nil
}

// noopConfigRepo is a no-op config repo for API key handler tests.
type noopConfigRepo struct{}

func (n *noopConfigRepo) Upsert(_ context.Context, _ string, _ []byte) (*domain.UserConfig, error) {
	return nil, nil
}
func (n *noopConfigRepo) GetByUserID(_ context.Context, _ string) (*domain.UserConfig, error) {
	return nil, nil
}
func (n *noopConfigRepo) DeleteByUserID(_ context.Context, _ string) error { return nil }

func setupAPIKeyRouter(t *testing.T) (*gin.Engine, *service.APIKeyService) {
	t.Helper()

	key := make([]byte, 32)
	_, _ = rand.Read(key)
	aes, _ := crypto.NewAES(key)

	// Mock Bitfinex server that returns valid wallet
	bfxServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`[["funding","USD",1000,0,800,null,null]]`))
	}))
	t.Cleanup(bfxServer.Close)
	bfx := bitfinex.NewClientWithBaseURL(bfxServer.Client(), bfxServer.URL)

	repo := &testAPIKeyRepo{keys: make(map[string]*testStoredKey)}
	svc := service.NewAPIKeyService(repo, &noopConfigRepo{}, aes, bfx, nil, zap.NewNop())
	h := NewAPIKeyHandler(svc)

	r := gin.New()
	r.Use(func(c *gin.Context) {
		c.Set(middleware.ContextUserID, "test-user-id")
		c.Next()
	})
	r.POST("/api-keys", h.Create)
	r.GET("/api-keys", h.List)
	r.GET("/apikeys/:id", h.GetByID)
	r.DELETE("/apikeys/:id", h.Delete)
	r.POST("/apikeys/:id/verify", h.Verify)

	return r, svc
}

func TestHandler_CreateAPIKey(t *testing.T) {
	r, _ := setupAPIKeyRouter(t)

	body, _ := json.Marshal(map[string]string{
		"apiKey":    "bfx-key-123",
		"apiSecret": "bfx-secret-456",
		"label":     "test",
	})

	req := httptest.NewRequest(http.MethodPost, "/api-keys", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusCreated {
		t.Fatalf("expected 201, got %d: %s", w.Code, w.Body.String())
	}

	var envelope map[string]interface{}
	_ = json.Unmarshal(w.Body.Bytes(), &envelope)
	resp := envelope["data"].(map[string]interface{})
	if resp["apiSecret"] != "****" {
		t.Errorf("expected masked secret, got %v", resp["apiSecret"])
	}
	if resp["apiKey"] != "bfx-key-123" {
		t.Errorf("expected apiKey bfx-key-123, got %v", resp["apiKey"])
	}
	if resp["exchangeStatus"] != "verified" {
		t.Errorf("expected exchangeStatus verified, got %v", resp["exchangeStatus"])
	}
}

func TestHandler_CreateAPIKey_MissingFields(t *testing.T) {
	r, _ := setupAPIKeyRouter(t)

	body, _ := json.Marshal(map[string]string{"label": "test"})
	req := httptest.NewRequest(http.MethodPost, "/api-keys", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", w.Code)
	}
}

func TestHandler_CreateAPIKey_Duplicate(t *testing.T) {
	r, _ := setupAPIKeyRouter(t)

	body, _ := json.Marshal(map[string]string{
		"apiKey": "k1", "apiSecret": "s1",
	})
	req1 := httptest.NewRequest(http.MethodPost, "/api-keys", bytes.NewReader(body))
	req1.Header.Set("Content-Type", "application/json")
	w1 := httptest.NewRecorder()
	r.ServeHTTP(w1, req1)

	body2, _ := json.Marshal(map[string]string{
		"apiKey": "k2", "apiSecret": "s2",
	})
	req2 := httptest.NewRequest(http.MethodPost, "/api-keys", bytes.NewReader(body2))
	req2.Header.Set("Content-Type", "application/json")
	w2 := httptest.NewRecorder()
	r.ServeHTTP(w2, req2)

	if w2.Code != http.StatusConflict {
		t.Fatalf("expected 409, got %d: %s", w2.Code, w2.Body.String())
	}
}

func TestHandler_ListAPIKeys_Empty(t *testing.T) {
	r, _ := setupAPIKeyRouter(t)

	req := httptest.NewRequest(http.MethodGet, "/api-keys", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", w.Code)
	}
	var envelope map[string]interface{}
	_ = json.Unmarshal(w.Body.Bytes(), &envelope)
	resp := envelope["data"].([]interface{})
	if len(resp) != 0 {
		t.Fatalf("expected empty array, got %d items", len(resp))
	}
}

func TestHandler_ListAPIKeys_IncludesExchangeStatus(t *testing.T) {
	r, _ := setupAPIKeyRouter(t)

	// Create a key first
	body, _ := json.Marshal(map[string]string{
		"apiKey": "k1", "apiSecret": "s1",
	})
	req := httptest.NewRequest(http.MethodPost, "/api-keys", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	// List keys
	req2 := httptest.NewRequest(http.MethodGet, "/api-keys", nil)
	w2 := httptest.NewRecorder()
	r.ServeHTTP(w2, req2)

	var envelope map[string]interface{}
	_ = json.Unmarshal(w2.Body.Bytes(), &envelope)
	data := envelope["data"].([]interface{})
	if len(data) != 1 {
		t.Fatalf("expected 1 key, got %d", len(data))
	}
	first := data[0].(map[string]interface{})
	if first["exchangeStatus"] == nil {
		t.Error("expected exchangeStatus in list response")
	}
}

func TestHandler_VerifyAPIKey(t *testing.T) {
	r, _ := setupAPIKeyRouter(t)

	// Create a key
	body, _ := json.Marshal(map[string]string{
		"apiKey": "k1", "apiSecret": "s1",
	})
	req := httptest.NewRequest(http.MethodPost, "/api-keys", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	var createEnvelope map[string]interface{}
	_ = json.Unmarshal(w.Body.Bytes(), &createEnvelope)
	createResp := createEnvelope["data"].(map[string]interface{})
	keyID := createResp["id"].(string)

	// Verify the key
	req2 := httptest.NewRequest(http.MethodPost, "/apikeys/"+keyID+"/verify", nil)
	w2 := httptest.NewRecorder()
	r.ServeHTTP(w2, req2)

	if w2.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w2.Code, w2.Body.String())
	}

	var verifyEnvelope map[string]interface{}
	_ = json.Unmarshal(w2.Body.Bytes(), &verifyEnvelope)
	verifyResp := verifyEnvelope["data"].(map[string]interface{})
	if verifyResp["status"] != "verified" {
		t.Errorf("expected status verified, got %v", verifyResp["status"])
	}
}

func TestHandler_VerifyAPIKey_NotFound(t *testing.T) {
	r, _ := setupAPIKeyRouter(t)

	req := httptest.NewRequest(http.MethodPost, "/apikeys/nonexistent/verify", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d: %s", w.Code, w.Body.String())
	}
}

func TestHandler_DeleteAPIKey(t *testing.T) {
	r, svc := setupAPIKeyRouter(t)

	key, _, _ := svc.Create(context.Background(), "test-user-id", "k", "s", "l")

	req := httptest.NewRequest(http.MethodDelete, "/apikeys/"+key.ID, nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d: %s", w.Code, w.Body.String())
	}
}

func TestHandler_DeleteAPIKey_NotFound(t *testing.T) {
	r, _ := setupAPIKeyRouter(t)

	req := httptest.NewRequest(http.MethodDelete, "/apikeys/nonexistent-id", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d: %s", w.Code, w.Body.String())
	}
}
