package service

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/bitfinex"
	"github.com/will/bfx-funding-bot/backend/internal/crypto"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func testEarningsCipher(t *testing.T) *crypto.AES {
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

func seedEarningsConfig(repo *mockConfigRepo, userID string) {
	cfg := domain.StrategyConfig{Currency: "USD"}
	configJSON, _ := json.Marshal(cfg)
	repo.Upsert(context.Background(), userID, configJSON)
}

func TestEarnings_ActiveCreditsWithHistory(t *testing.T) {
	cipher := testEarningsCipher(t)
	secret, _ := cipher.Encrypt([]byte("test-secret"))

	now := time.Now().UnixMilli()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case containsPath(r.URL.Path, "credits"):
			// Two active credits: 500 @ 0.0002/day, 300 @ 0.0003/day
			// Format: [ID, SYMBOL, SIDE, MTS_CREATE, MTS_UPDATE, AMOUNT, FLAGS, STATUS, RATE_TYPE, null, null, RATE, PERIOD, MTS_OPENING, ...]
			w.Write([]byte(fmt.Sprintf(`[
				[11111,"fUSD",0,%d,%d,500,0,"ACTIVE",null,null,null,0.0002,7,%d,null,0,0,null,0,null,null,null],
				[22222,"fUSD",0,%d,%d,300,0,"ACTIVE",null,null,null,0.0003,14,%d,null,0,0,null,0,null,null,null]
			]`, now, now, now, now, now, now)))
		case containsPath(r.URL.Path, "ledgers"):
			// Historical earnings
			w.Write([]byte(fmt.Sprintf(`[
				[100001,"fUSD",null,%d,null,0.10,10000.10,null,"Margin Funding Payment"],
				[100002,"fUSD",null,%d,null,0.09,10000.00,null,"Margin Funding Payment"]
			]`, now, now)))
		}
	}))
	defer ts.Close()

	bfx := bitfinex.NewClientWithBaseURL(ts.Client(), ts.URL)
	apiKeyRepo := newMockAPIKeyRepo()
	apiKeyRepo.keys["key-user1"] = &storedKey{
		key:             &domain.APIKey{ID: "key-user1", UserID: "user1", APIKey: "k", ExchangeStatus: "verified"},
		encryptedSecret: secret,
	}
	configRepo := newMockConfigRepo()
	seedEarningsConfig(configRepo, "user1")

	svc := NewEarningsService(bfx, apiKeyRepo, configRepo, cipher)
	summary, err := svc.GetEarnings(context.Background(), "user1")
	if err != nil {
		t.Fatal(err)
	}

	// estimated_daily = 500*0.0002 + 300*0.0003 = 0.10 + 0.09 = 0.19
	if math.Abs(summary.EstimatedDailyEarning-0.19) > 0.0001 {
		t.Errorf("expected daily earning ~0.19, got %f", summary.EstimatedDailyEarning)
	}

	// weighted_apy = (0.19 / 800) * 365 ≈ 0.086687
	expectedAPY := (0.19 / 800.0) * 365
	if math.Abs(summary.WeightedAPY-expectedAPY) > 0.001 {
		t.Errorf("expected APY ~%f, got %f", expectedAPY, summary.WeightedAPY)
	}

	if summary.ActiveCredits != 2 {
		t.Errorf("expected 2 active credits, got %d", summary.ActiveCredits)
	}
	if summary.TotalLent != 800 {
		t.Errorf("expected total lent 800, got %f", summary.TotalLent)
	}

	// Both 7d and 30d queries hit the same mock, each returns 0.10 + 0.09 = 0.19
	if math.Abs(summary.Earnings7d-0.19) > 0.001 {
		t.Errorf("expected earnings_7d ~0.19, got %f", summary.Earnings7d)
	}
	if math.Abs(summary.Earnings30d-0.19) > 0.001 {
		t.Errorf("expected earnings_30d ~0.19, got %f", summary.Earnings30d)
	}
}

func TestEarnings_NoAPIKey(t *testing.T) {
	cipher := testEarningsCipher(t)

	bfx := bitfinex.NewClientWithBaseURL(http.DefaultClient, "http://unused")
	apiKeyRepo := newMockAPIKeyRepo()
	configRepo := newMockConfigRepo()

	svc := NewEarningsService(bfx, apiKeyRepo, configRepo, cipher)
	summary, err := svc.GetEarnings(context.Background(), "no-user")
	if err != nil {
		t.Fatal(err)
	}

	if summary.EstimatedDailyEarning != 0 {
		t.Errorf("expected 0 daily earning, got %f", summary.EstimatedDailyEarning)
	}
	if summary.WeightedAPY != 0 {
		t.Errorf("expected 0 APY, got %f", summary.WeightedAPY)
	}
}

func TestEarnings_NoCredits(t *testing.T) {
	cipher := testEarningsCipher(t)
	secret, _ := cipher.Encrypt([]byte("test-secret"))

	now := time.Now().UnixMilli()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case containsPath(r.URL.Path, "credits"):
			w.Write([]byte(`[]`))
		case containsPath(r.URL.Path, "ledgers"):
			w.Write([]byte(fmt.Sprintf(`[[100001,"fUSD",null,%d,null,0.50,10000.50,null,"Margin Funding Payment"]]`, now)))
		}
	}))
	defer ts.Close()

	bfx := bitfinex.NewClientWithBaseURL(ts.Client(), ts.URL)
	apiKeyRepo := newMockAPIKeyRepo()
	apiKeyRepo.keys["key-user1"] = &storedKey{
		key:             &domain.APIKey{ID: "key-user1", UserID: "user1", APIKey: "k", ExchangeStatus: "verified"},
		encryptedSecret: secret,
	}
	configRepo := newMockConfigRepo()
	seedEarningsConfig(configRepo, "user1")

	svc := NewEarningsService(bfx, apiKeyRepo, configRepo, cipher)
	summary, err := svc.GetEarnings(context.Background(), "user1")
	if err != nil {
		t.Fatal(err)
	}

	if summary.EstimatedDailyEarning != 0 {
		t.Errorf("expected 0 daily earning, got %f", summary.EstimatedDailyEarning)
	}
	if summary.WeightedAPY != 0 {
		t.Errorf("expected 0 APY, got %f", summary.WeightedAPY)
	}
	// Historical earnings should still be available
	if summary.Earnings7d != 0.50 {
		t.Errorf("expected earnings_7d 0.50, got %f", summary.Earnings7d)
	}
}

func TestEarnings_PartialFailure(t *testing.T) {
	cipher := testEarningsCipher(t)
	secret, _ := cipher.Encrypt([]byte("test-secret"))

	now := time.Now().UnixMilli()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case containsPath(r.URL.Path, "credits"):
			w.Write([]byte(fmt.Sprintf(`[[11111,"fUSD",0,%d,%d,500,0,"ACTIVE",null,null,null,0.0002,7,%d,null,0,0,null,0,null,null,null]]`, now, now, now)))
		case containsPath(r.URL.Path, "ledgers"):
			w.WriteHeader(http.StatusInternalServerError)
			w.Write([]byte(`["error",10000,"Internal Server Error"]`))
		}
	}))
	defer ts.Close()

	bfx := bitfinex.NewClientWithBaseURL(ts.Client(), ts.URL)
	apiKeyRepo := newMockAPIKeyRepo()
	apiKeyRepo.keys["key-user1"] = &storedKey{
		key:             &domain.APIKey{ID: "key-user1", UserID: "user1", APIKey: "k", ExchangeStatus: "verified"},
		encryptedSecret: secret,
	}
	configRepo := newMockConfigRepo()
	seedEarningsConfig(configRepo, "user1")

	svc := NewEarningsService(bfx, apiKeyRepo, configRepo, cipher)
	summary, err := svc.GetEarnings(context.Background(), "user1")
	if err != nil {
		t.Fatal(err)
	}

	// Credits should work despite ledger failure
	if summary.EstimatedDailyEarning != 0.10 {
		t.Errorf("expected daily earning 0.10, got %f", summary.EstimatedDailyEarning)
	}
	// Earnings should be 0 (ledger failed gracefully)
	if summary.Earnings7d != 0 {
		t.Errorf("expected earnings_7d 0, got %f", summary.Earnings7d)
	}
}
