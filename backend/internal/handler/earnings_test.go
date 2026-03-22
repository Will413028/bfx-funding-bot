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
	"github.com/will/bfx-funding-bot/backend/internal/lending/quota"
	"github.com/will/bfx-funding-bot/backend/internal/middleware"
	"github.com/will/bfx-funding-bot/backend/internal/service"
)

func setupEarningsRouter(t *testing.T) *gin.Engine {
	t.Helper()

	key := make([]byte, 32)
	_, _ = rand.Read(key)
	aes, _ := crypto.NewAES(key)
	secret, _ := aes.Encrypt([]byte("test-secret"))

	now := time.Now().UnixMilli()
	bfxServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case containsEarn(r.URL.Path, "credits"):
			_, _ = fmt.Fprintf(w, `[[11111,"fUSD",0,%d,%d,500,0,"ACTIVE",null,null,null,0.0002,7,%d,null,0,0,null,0,null,null,null]]`, now, now, now)
		case containsEarn(r.URL.Path, "ledgers"):
			_, _ = fmt.Fprintf(w, `[[100001,"fUSD",null,%d,null,0.10,10000.10,null,"Margin Funding Payment"]]`, now)
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

	svc := service.NewEarningsService(bfx, apiKeyRepo, configRepo, aes, quota.NewRateLimiterPool(1000, 1000))
	h := NewEarningsHandler(svc)

	r := gin.New()
	r.Use(func(c *gin.Context) {
		c.Set(middleware.ContextUserID, "test-user-id")
		c.Next()
	})
	r.GET("/earnings", h.Get)

	return r
}

func TestEarningsHandler_Success(t *testing.T) {
	r := setupEarningsRouter(t)

	req := httptest.NewRequest("GET", "/earnings", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w.Code, w.Body.String())
	}

	var envelope struct {
		Data service.EarningsSummary `json:"data"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &envelope); err != nil {
		t.Fatal(err)
	}
	resp := envelope.Data

	if resp.ActiveCredits != 1 {
		t.Errorf("expected 1 active credit, got %d", resp.ActiveCredits)
	}
	if resp.Currency != "USD" {
		t.Errorf("expected currency USD, got %s", resp.Currency)
	}
	if resp.EstimatedDailyEarning == 0 {
		t.Error("expected non-zero estimated daily earning")
	}
}

func containsEarn(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}
