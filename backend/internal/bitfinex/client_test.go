package bitfinex

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func setupTestServer(handler http.HandlerFunc) (*Client, *httptest.Server) {
	ts := httptest.NewServer(handler)
	client := NewClient(ts.Client())
	return client, ts
}

func TestVerifyCredentials_Success(t *testing.T) {
	client, ts := setupTestServer(func(w http.ResponseWriter, r *http.Request) {
		// Verify auth headers are set
		if r.Header.Get("bfx-apikey") == "" {
			t.Error("missing bfx-apikey header")
		}
		if r.Header.Get("bfx-nonce") == "" {
			t.Error("missing bfx-nonce header")
		}
		if r.Header.Get("bfx-signature") == "" {
			t.Error("missing bfx-signature header")
		}
		w.Write([]byte(`[["funding","USD",1000,0,1000,null,null]]`))
	})
	defer ts.Close()

	// Override baseURL for test
	origDoAuth := overrideBaseURL(client, ts.URL)
	defer origDoAuth()

	err := client.VerifyCredentials(context.Background(), "test-key", "test-secret")
	if err != nil {
		t.Errorf("expected no error, got: %v", err)
	}
}

func TestVerifyCredentials_InvalidKey(t *testing.T) {
	client, ts := setupTestServer(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`["error",10100,"apikey: invalid"]`))
	})
	defer ts.Close()

	origDoAuth := overrideBaseURL(client, ts.URL)
	defer origDoAuth()

	err := client.VerifyCredentials(context.Background(), "bad-key", "bad-secret")
	if err == nil {
		t.Error("expected error for invalid key")
	}
	appErr, ok := err.(*domain.AppError)
	if !ok {
		t.Fatalf("expected *domain.AppError, got %T", err)
	}
	if appErr.Code != "INVALID_API_KEY" {
		t.Errorf("expected INVALID_API_KEY, got %s", appErr.Code)
	}
}

func TestGetFundingBalance_Success(t *testing.T) {
	client, ts := setupTestServer(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`[["exchange","USD",500,0,500,null,null],["funding","USD",1000,0,800,null,null],["funding","ETH",10,0,10,null,null]]`))
	})
	defer ts.Close()

	origDoAuth := overrideBaseURL(client, ts.URL)
	defer origDoAuth()

	wallet, err := client.GetFundingBalance(context.Background(), "key", "secret", "USD")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if wallet.Currency != "USD" {
		t.Errorf("expected USD, got %s", wallet.Currency)
	}
	if wallet.Balance != 1000 {
		t.Errorf("expected balance 1000, got %f", wallet.Balance)
	}
	if wallet.BalanceAvailable != 800 {
		t.Errorf("expected available 800, got %f", wallet.BalanceAvailable)
	}
}

func TestGetFundingBalance_NotFound(t *testing.T) {
	client, ts := setupTestServer(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`[["exchange","USD",500,0,500,null,null]]`))
	})
	defer ts.Close()

	origDoAuth := overrideBaseURL(client, ts.URL)
	defer origDoAuth()

	wallet, err := client.GetFundingBalance(context.Background(), "key", "secret", "BTC")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if wallet.Balance != 0 {
		t.Errorf("expected zero balance, got %f", wallet.Balance)
	}
}

func TestSubmitFundingOffer_Success(t *testing.T) {
	client, ts := setupTestServer(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`[1234,"fon-req",null,null,[12345,"fUSD",1700000000000,1700000000000,50,50,"LIMIT",null,null,null,null,null,null,null,0.0001,2,0,0,null,0,null],null,"SUCCESS",null]`))
	})
	defer ts.Close()

	origDoAuth := overrideBaseURL(client, ts.URL)
	defer origDoAuth()

	offer, err := client.SubmitFundingOffer(context.Background(), "key", "secret", domain.OfferParams{
		Currency: "USD",
		Amount:   50,
		Rate:     0.0001,
		Period:   2,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if offer.ID != 12345 {
		t.Errorf("expected ID 12345, got %d", offer.ID)
	}
	if offer.Amount != 50 {
		t.Errorf("expected amount 50, got %f", offer.Amount)
	}
}

func TestSubmitFundingOffer_Error(t *testing.T) {
	client, ts := setupTestServer(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`["error",10020,"Not enough balance"]`))
	})
	defer ts.Close()

	origDoAuth := overrideBaseURL(client, ts.URL)
	defer origDoAuth()

	_, err := client.SubmitFundingOffer(context.Background(), "key", "secret", domain.OfferParams{
		Currency: "USD",
		Amount:   50,
		Rate:     0.0001,
		Period:   2,
	})
	if err == nil {
		t.Error("expected error")
	}
}

func TestCancelFundingOffer_Success(t *testing.T) {
	client, ts := setupTestServer(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`[1234,"foc-req",null,null,[12345,"fUSD",1700000000000,1700000000000,50,50,"LIMIT",null,null,null,"EXECUTED",null,null,null,0.0001,2,0,0,null,0,null],null,"SUCCESS",null]`))
	})
	defer ts.Close()

	origDoAuth := overrideBaseURL(client, ts.URL)
	defer origDoAuth()

	err := client.CancelFundingOffer(context.Background(), "key", "secret", 12345)
	if err != nil {
		t.Errorf("unexpected error: %v", err)
	}
}

func TestGetActiveFundingOffers_Success(t *testing.T) {
	client, ts := setupTestServer(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`[[12345,"fUSD",1700000000000,1700000000000,50,50,"LIMIT",null,null,0,"ACTIVE",null,null,null,0.0001,2,0,0,null,0,null],[12346,"fUSD",1700000000000,1700000000000,100,100,"LIMIT",null,null,0,"ACTIVE",null,null,null,0.0002,5,0,0,null,0,null]]`))
	})
	defer ts.Close()

	origDoAuth := overrideBaseURL(client, ts.URL)
	defer origDoAuth()

	offers, err := client.GetActiveFundingOffers(context.Background(), "key", "secret", "USD")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(offers) != 2 {
		t.Fatalf("expected 2 offers, got %d", len(offers))
	}
	if offers[0].ID != 12345 {
		t.Errorf("expected first offer ID 12345, got %d", offers[0].ID)
	}
	if offers[1].Rate != 0.0002 {
		t.Errorf("expected second offer rate 0.0002, got %f", offers[1].Rate)
	}
}

func TestGetActiveFundingOffers_Empty(t *testing.T) {
	client, ts := setupTestServer(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`[]`))
	})
	defer ts.Close()

	origDoAuth := overrideBaseURL(client, ts.URL)
	defer origDoAuth()

	offers, err := client.GetActiveFundingOffers(context.Background(), "key", "secret", "USD")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if offers == nil {
		t.Error("expected empty slice, got nil")
	}
	if len(offers) != 0 {
		t.Errorf("expected 0 offers, got %d", len(offers))
	}
}

func TestGetActiveFundingCredits_Success(t *testing.T) {
	client, ts := setupTestServer(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`[[99999,"fUSD",0,1700000000000,1700000000000,200,0,"ACTIVE","FIXED",null,null,0.00015,7,1700000000000,1700000000000,0,0,null,1,null,0,"tETHUSD"]]`))
	})
	defer ts.Close()

	origDoAuth := overrideBaseURL(client, ts.URL)
	defer origDoAuth()

	credits, err := client.GetActiveFundingCredits(context.Background(), "key", "secret", "USD")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(credits) != 1 {
		t.Fatalf("expected 1 credit, got %d", len(credits))
	}
	if credits[0].ID != 99999 {
		t.Errorf("expected ID 99999, got %d", credits[0].ID)
	}
	if credits[0].Rate != 0.00015 {
		t.Errorf("expected rate 0.00015, got %f", credits[0].Rate)
	}
	if !credits[0].AutoRenew {
		t.Error("expected AutoRenew to be true")
	}
}

func TestGetActiveFundingCredits_Empty(t *testing.T) {
	client, ts := setupTestServer(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`[]`))
	})
	defer ts.Close()

	origDoAuth := overrideBaseURL(client, ts.URL)
	defer origDoAuth()

	credits, err := client.GetActiveFundingCredits(context.Background(), "key", "secret", "USD")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if credits == nil {
		t.Error("expected empty slice, got nil")
	}
	if len(credits) != 0 {
		t.Errorf("expected 0 credits, got %d", len(credits))
	}
}

func TestParseError_BitfinexError(t *testing.T) {
	err := parseError([]byte(`["error",10114,"nonce: small"]`))
	if err == nil {
		t.Fatal("expected error")
	}
}

func TestParseError_NonError(t *testing.T) {
	err := parseError([]byte(`[["funding","USD",1000,0,1000]]`))
	if err != nil {
		t.Errorf("expected no error, got: %v", err)
	}
}

func TestGetFundingEarnings_Success(t *testing.T) {
	client, ts := setupTestServer(func(w http.ResponseWriter, r *http.Request) {
		// Ledger response: [ID, CURRENCY, null, MTS, null, AMOUNT, BALANCE, null, DESCRIPTION]
		w.Write([]byte(`[
			[100001,"fUSD",null,1709856000000,null,0.52,10000.52,null,"Margin Funding Payment on wallet funding"],
			[100002,"fUSD",null,1709769600000,null,0.48,10000.00,null,"Margin Funding Payment on wallet funding"]
		]`))
	})
	defer ts.Close()

	origDoAuth := overrideBaseURL(client, ts.URL)
	defer origDoAuth()

	start := time.Date(2024, 3, 1, 0, 0, 0, 0, time.UTC)
	end := time.Date(2024, 3, 8, 0, 0, 0, 0, time.UTC)

	earnings, err := client.GetFundingEarnings(context.Background(), "key", "secret", "USD", start, end)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(earnings) != 2 {
		t.Fatalf("expected 2 earnings, got %d", len(earnings))
	}
	if earnings[0].Amount != 0.52 {
		t.Errorf("expected amount 0.52, got %f", earnings[0].Amount)
	}
	if earnings[1].Amount != 0.48 {
		t.Errorf("expected amount 0.48, got %f", earnings[1].Amount)
	}
}

func TestGetFundingEarnings_Empty(t *testing.T) {
	client, ts := setupTestServer(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`[]`))
	})
	defer ts.Close()

	origDoAuth := overrideBaseURL(client, ts.URL)
	defer origDoAuth()

	start := time.Date(2024, 3, 1, 0, 0, 0, 0, time.UTC)
	end := time.Date(2024, 3, 8, 0, 0, 0, 0, time.UTC)

	earnings, err := client.GetFundingEarnings(context.Background(), "key", "secret", "USD", start, end)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(earnings) != 0 {
		t.Errorf("expected 0 earnings, got %d", len(earnings))
	}
}

// overrideBaseURL patches the client to use the test server URL.
// Returns a cleanup function.
func overrideBaseURL(c *Client, testURL string) func() {
	c.baseURLOverride = testURL
	return func() {
		c.baseURLOverride = ""
	}
}
