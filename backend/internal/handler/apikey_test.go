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

func setupAPIKeyRouter(t *testing.T) (*gin.Engine, *service.APIKeyService) {
	t.Helper()

	key := make([]byte, 32)
	rand.Read(key)
	aes, _ := crypto.NewAES(key)

	// Mock Bitfinex server that returns valid wallet
	bfxServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`[["funding","USD",1000,0,800,null,null]]`))
	}))
	t.Cleanup(bfxServer.Close)
	bfx := bitfinex.NewClientWithBaseURL(bfxServer.Client(), bfxServer.URL)

	repo := &testAPIKeyRepo{keys: make(map[string]*testStoredKey)}
	svc := service.NewAPIKeyService(repo, aes, bfx)
	h := NewAPIKeyHandler(svc)

	r := gin.New()
	r.Use(func(c *gin.Context) {
		c.Set(middleware.ContextUserID, "test-user-id")
		c.Next()
	})
	r.POST("/apikeys", h.Create)
	r.GET("/apikeys", h.List)
	r.GET("/apikeys/:id", h.GetByID)
	r.DELETE("/apikeys/:id", h.Delete)
	r.POST("/apikeys/:id/verify", h.Verify)

	return r, svc
}

func TestHandler_CreateAPIKey(t *testing.T) {
	r, _ := setupAPIKeyRouter(t)

	body, _ := json.Marshal(map[string]string{
		"api_key":    "bfx-key-123",
		"api_secret": "bfx-secret-456",
		"label":      "test",
	})

	req := httptest.NewRequest(http.MethodPost, "/apikeys", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusCreated {
		t.Fatalf("expected 201, got %d: %s", w.Code, w.Body.String())
	}

	var resp map[string]interface{}
	json.Unmarshal(w.Body.Bytes(), &resp)
	if resp["api_secret"] != "****" {
		t.Errorf("expected masked secret, got %v", resp["api_secret"])
	}
	if resp["api_key"] != "bfx-key-123" {
		t.Errorf("expected api_key bfx-key-123, got %v", resp["api_key"])
	}
	if resp["exchange_status"] != "verified" {
		t.Errorf("expected exchange_status verified, got %v", resp["exchange_status"])
	}
}

func TestHandler_CreateAPIKey_MissingFields(t *testing.T) {
	r, _ := setupAPIKeyRouter(t)

	body, _ := json.Marshal(map[string]string{"label": "test"})
	req := httptest.NewRequest(http.MethodPost, "/apikeys", bytes.NewReader(body))
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
		"api_key": "k1", "api_secret": "s1",
	})
	req1 := httptest.NewRequest(http.MethodPost, "/apikeys", bytes.NewReader(body))
	req1.Header.Set("Content-Type", "application/json")
	w1 := httptest.NewRecorder()
	r.ServeHTTP(w1, req1)

	body2, _ := json.Marshal(map[string]string{
		"api_key": "k2", "api_secret": "s2",
	})
	req2 := httptest.NewRequest(http.MethodPost, "/apikeys", bytes.NewReader(body2))
	req2.Header.Set("Content-Type", "application/json")
	w2 := httptest.NewRecorder()
	r.ServeHTTP(w2, req2)

	if w2.Code != http.StatusConflict {
		t.Fatalf("expected 409, got %d: %s", w2.Code, w2.Body.String())
	}
}

func TestHandler_ListAPIKeys_Empty(t *testing.T) {
	r, _ := setupAPIKeyRouter(t)

	req := httptest.NewRequest(http.MethodGet, "/apikeys", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", w.Code)
	}
	var resp []interface{}
	json.Unmarshal(w.Body.Bytes(), &resp)
	if len(resp) != 0 {
		t.Fatalf("expected empty array, got %d items", len(resp))
	}
}

func TestHandler_ListAPIKeys_IncludesExchangeStatus(t *testing.T) {
	r, _ := setupAPIKeyRouter(t)

	// Create a key first
	body, _ := json.Marshal(map[string]string{
		"api_key": "k1", "api_secret": "s1",
	})
	req := httptest.NewRequest(http.MethodPost, "/apikeys", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	// List keys
	req2 := httptest.NewRequest(http.MethodGet, "/apikeys", nil)
	w2 := httptest.NewRecorder()
	r.ServeHTTP(w2, req2)

	var resp []map[string]interface{}
	json.Unmarshal(w2.Body.Bytes(), &resp)
	if len(resp) != 1 {
		t.Fatalf("expected 1 key, got %d", len(resp))
	}
	if resp[0]["exchange_status"] == nil {
		t.Error("expected exchange_status in list response")
	}
}

func TestHandler_VerifyAPIKey(t *testing.T) {
	r, _ := setupAPIKeyRouter(t)

	// Create a key
	body, _ := json.Marshal(map[string]string{
		"api_key": "k1", "api_secret": "s1",
	})
	req := httptest.NewRequest(http.MethodPost, "/apikeys", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	var createResp map[string]interface{}
	json.Unmarshal(w.Body.Bytes(), &createResp)
	keyID := createResp["id"].(string)

	// Verify the key
	req2 := httptest.NewRequest(http.MethodPost, "/apikeys/"+keyID+"/verify", nil)
	w2 := httptest.NewRecorder()
	r.ServeHTTP(w2, req2)

	if w2.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w2.Code, w2.Body.String())
	}

	var verifyResp map[string]interface{}
	json.Unmarshal(w2.Body.Bytes(), &verifyResp)
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
