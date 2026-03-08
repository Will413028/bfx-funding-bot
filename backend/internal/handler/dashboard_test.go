package handler

import (
	"crypto/rand"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/will/bfx-funding-bot/backend/internal/bitfinex"
	"github.com/will/bfx-funding-bot/backend/internal/crypto"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/middleware"
	"github.com/will/bfx-funding-bot/backend/internal/service"
)

func setupDashboardRouter(t *testing.T) *gin.Engine {
	t.Helper()

	key := make([]byte, 32)
	rand.Read(key)
	aes, _ := crypto.NewAES(key)
	secret, _ := aes.Encrypt([]byte("test-secret"))

	now := time.Now().UnixMilli()
	bfxServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case containsDash(r.URL.Path, "wallets"):
			w.Write([]byte(`[["funding","USD",1000,0,800,null,null]]`))
		case containsDash(r.URL.Path, "offers"):
			w.Write([]byte(fmt.Sprintf(`[[12345,"fUSD",%d,%d,500,500,"LIMIT",null,null,0,"ACTIVE",null,null,null,0.0001,2,0,0,null,0,null]]`, now, now)))
		case containsDash(r.URL.Path, "credits"):
			w.Write([]byte(`[]`))
		}
	}))
	t.Cleanup(bfxServer.Close)

	bfx := bitfinex.NewClientWithBaseURL(bfxServer.Client(), bfxServer.URL)
	apiKeyRepo := &testAPIKeyRepo{keys: map[string]*testStoredKey{
		"key-test-user-id": {
			domainKey: &domain.APIKey{
				ID: "key-test-user-id", UserID: "test-user-id", APIKey: "bfx-key",
				ExchangeStatus: "verified",
			},
			encryptedSecret: secret,
		},
	}}
	configRepo := &testConfigRepo{
		configs: map[string]*domain.UserConfig{
			"test-user-id": {UserID: "test-user-id", Config: domain.StrategyConfig{Currency: "USD"}},
		},
	}

	svc := service.NewDashboardService(bfx, apiKeyRepo, configRepo, aes)
	h := NewDashboardHandler(svc)

	r := gin.New()
	authed := r.Group("", func(c *gin.Context) {
		c.Set(middleware.ContextUserID, "test-user-id")
		c.Next()
	})
	authed.GET("/dashboard", h.Get)

	// Unprotected route for no-userID test
	r.GET("/dashboard-noauth", h.Get)

	return r
}

func TestDashboardHandler_Success(t *testing.T) {
	r := setupDashboardRouter(t)

	req := httptest.NewRequest("GET", "/dashboard", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w.Code, w.Body.String())
	}

	var resp service.DashboardSummary
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}

	if !resp.EngineReady {
		t.Error("expected engine_ready to be true")
	}
	if resp.Wallet == nil {
		t.Fatal("expected wallet to be non-nil")
	}
	if resp.Wallet.BalanceAvailable != 800 {
		t.Errorf("expected balance_available 800, got %f", resp.Wallet.BalanceAvailable)
	}
	if len(resp.Offers) != 1 {
		t.Errorf("expected 1 offer, got %d", len(resp.Offers))
	}
}

func TestDashboardHandler_NoUserID(t *testing.T) {
	r := setupDashboardRouter(t)

	req := httptest.NewRequest("GET", "/dashboard-noauth", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w.Code, w.Body.String())
	}

	var resp service.DashboardSummary
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}

	if resp.EngineReady {
		t.Error("expected engine_ready to be false for empty userID")
	}
}

func containsDash(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}
