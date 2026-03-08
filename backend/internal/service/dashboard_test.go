package service

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/bitfinex"
	"github.com/will/bfx-funding-bot/backend/internal/crypto"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

type mockSnapshotCache struct {
	snapshots map[string]*domain.MarketSnapshot
}

func newMockSnapshotCache() *mockSnapshotCache {
	return &mockSnapshotCache{snapshots: make(map[string]*domain.MarketSnapshot)}
}

func (m *mockSnapshotCache) Set(_ context.Context, symbol string, snapshot *domain.MarketSnapshot, _ time.Duration) error {
	m.snapshots[symbol] = snapshot
	return nil
}

func (m *mockSnapshotCache) Get(_ context.Context, symbol string) (*domain.MarketSnapshot, error) {
	return m.snapshots[symbol], nil
}

func (m *mockSnapshotCache) Delete(_ context.Context, symbol string) error {
	delete(m.snapshots, symbol)
	return nil
}

func testDashboardCipher(t *testing.T) *crypto.AES {
	t.Helper()
	key := make([]byte, 32)
	for i := range key {
		key[i] = byte(i)
	}
	c, err := crypto.NewAES(key)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func seedConfig(repo *mockConfigRepo, userID string, cfg domain.StrategyConfig) {
	configJSON, _ := json.Marshal(cfg)
	repo.Upsert(context.Background(), userID, configJSON)
}

func TestDashboard_VerifiedKeyWithConfig(t *testing.T) {
	cipher := testDashboardCipher(t)
	secret, _ := cipher.Encrypt([]byte("test-secret"))

	now := time.Now().UnixMilli()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case containsPath(r.URL.Path, "wallets"):
			w.Write([]byte(`[["funding","USD",1000,0,800,null,null]]`))
		case containsPath(r.URL.Path, "offers"):
			w.Write([]byte(fmt.Sprintf(`[[12345,"fUSD",%d,%d,500,500,"LIMIT",null,null,0,"ACTIVE",null,null,null,0.0001,2,0,0,null,0,null]]`, now, now)))
		case containsPath(r.URL.Path, "credits"):
			w.Write([]byte(fmt.Sprintf(`[[67890,"fUSD",%d,%d,300,null,0.0002,7,null,0,"ACTIVE",null,null,null,0,0,null,0,null,1,null,%d]]`, now, now, now)))
		}
	}))
	defer ts.Close()

	bfx := bitfinex.NewClientWithBaseURL(ts.Client(), ts.URL)
	apiKeyRepo := newMockAPIKeyRepo()
	apiKeyRepo.keys["key-user1"] = &storedKey{
		key: &domain.APIKey{
			ID: "key-user1", UserID: "user1", APIKey: "bfx-key",
			ExchangeStatus: "verified",
		},
		encryptedSecret: secret,
	}
	configRepo := newMockConfigRepo()
	seedConfig(configRepo, "user1", domain.StrategyConfig{Currency: "USD"})

	cache := newMockSnapshotCache()
	cache.snapshots["fUSD"] = &domain.MarketSnapshot{
		Symbol:      "fUSD",
		FRR:         0.00045,
		Regime:      domain.RegimeContango,
		MDC:         domain.MDCResult{Score: 0.72},
		FlashFreeze: false,
		Timestamp:   time.Now(),
	}

	svc := NewDashboardService(bfx, apiKeyRepo, configRepo, cache, cipher)
	summary, err := svc.GetSummary(context.Background(), "user1")
	if err != nil {
		t.Fatal(err)
	}

	if !summary.EngineReady {
		t.Error("expected engine_ready to be true")
	}
	if summary.Wallet == nil {
		t.Fatal("expected wallet to be non-nil")
	}
	if summary.Wallet.BalanceAvailable != 800 {
		t.Errorf("expected available balance 800, got %f", summary.Wallet.BalanceAvailable)
	}
	if len(summary.Offers) != 1 {
		t.Errorf("expected 1 offer, got %d", len(summary.Offers))
	}
	if len(summary.Credits) != 1 {
		t.Errorf("expected 1 credit, got %d", len(summary.Credits))
	}
	if summary.Market == nil {
		t.Fatal("expected market to be non-nil")
	}
	if summary.Market.FRR != 0.00045 {
		t.Errorf("expected FRR 0.00045, got %f", summary.Market.FRR)
	}
	if summary.Market.Regime != domain.RegimeContango {
		t.Errorf("expected regime contango, got %s", summary.Market.Regime)
	}
	if summary.Market.MDCScore != 0.72 {
		t.Errorf("expected MDC score 0.72, got %f", summary.Market.MDCScore)
	}
}

func TestDashboard_NoAPIKey(t *testing.T) {
	cipher := testDashboardCipher(t)

	bfx := bitfinex.NewClientWithBaseURL(http.DefaultClient, "http://unused")
	apiKeyRepo := newMockAPIKeyRepo()
	configRepo := newMockConfigRepo()

	svc := NewDashboardService(bfx, apiKeyRepo, configRepo, newMockSnapshotCache(), cipher)
	summary, err := svc.GetSummary(context.Background(), "no-user")
	if err != nil {
		t.Fatal(err)
	}

	if summary.EngineReady {
		t.Error("expected engine_ready to be false")
	}
	if summary.Wallet != nil {
		t.Error("expected wallet to be nil")
	}
	if len(summary.Offers) != 0 {
		t.Errorf("expected 0 offers, got %d", len(summary.Offers))
	}
}

func TestDashboard_UnverifiedKey(t *testing.T) {
	cipher := testDashboardCipher(t)
	secret, _ := cipher.Encrypt([]byte("test-secret"))

	bfx := bitfinex.NewClientWithBaseURL(http.DefaultClient, "http://unused")
	apiKeyRepo := newMockAPIKeyRepo()
	apiKeyRepo.keys["key-user1"] = &storedKey{
		key: &domain.APIKey{
			ID: "key-user1", UserID: "user1", APIKey: "bfx-key",
			ExchangeStatus: "unverified",
		},
		encryptedSecret: secret,
	}
	configRepo := newMockConfigRepo()

	svc := NewDashboardService(bfx, apiKeyRepo, configRepo, newMockSnapshotCache(), cipher)
	summary, err := svc.GetSummary(context.Background(), "user1")
	if err != nil {
		t.Fatal(err)
	}

	if summary.EngineReady {
		t.Error("expected engine_ready to be false")
	}
	if summary.Wallet != nil {
		t.Error("expected wallet to be nil for unverified key")
	}
}

func TestDashboard_PartialBitfinexFailure(t *testing.T) {
	cipher := testDashboardCipher(t)
	secret, _ := cipher.Encrypt([]byte("test-secret"))

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case containsPath(r.URL.Path, "wallets"):
			w.Write([]byte(`[["funding","USD",500,0,400,null,null]]`))
		case containsPath(r.URL.Path, "offers"):
			w.WriteHeader(http.StatusInternalServerError)
			w.Write([]byte(`["error",10000,"Internal Server Error"]`))
		case containsPath(r.URL.Path, "credits"):
			w.Write([]byte(`[]`))
		}
	}))
	defer ts.Close()

	bfx := bitfinex.NewClientWithBaseURL(ts.Client(), ts.URL)
	apiKeyRepo := newMockAPIKeyRepo()
	apiKeyRepo.keys["key-user1"] = &storedKey{
		key: &domain.APIKey{
			ID: "key-user1", UserID: "user1", APIKey: "bfx-key",
			ExchangeStatus: "verified",
		},
		encryptedSecret: secret,
	}
	configRepo := newMockConfigRepo()
	seedConfig(configRepo, "user1", domain.StrategyConfig{Currency: "USD"})

	svc := NewDashboardService(bfx, apiKeyRepo, configRepo, newMockSnapshotCache(), cipher)
	summary, err := svc.GetSummary(context.Background(), "user1")
	if err != nil {
		t.Fatal(err)
	}

	if summary.Wallet == nil {
		t.Error("expected wallet to be non-nil despite offers failure")
	}
	if summary.Wallet.BalanceAvailable != 400 {
		t.Errorf("expected available balance 400, got %f", summary.Wallet.BalanceAvailable)
	}
	if len(summary.Offers) != 0 {
		t.Errorf("expected 0 offers (failed gracefully), got %d", len(summary.Offers))
	}
}

func containsPath(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}
