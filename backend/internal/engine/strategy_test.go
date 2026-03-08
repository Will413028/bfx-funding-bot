package engine

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"go.uber.org/zap"

	"github.com/will/bfx-funding-bot/backend/internal/bitfinex"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func testConfig() domain.StrategyConfig {
	return domain.StrategyConfig{
		Currency:  "USD",
		Amount:    domain.AmountConfig{Min: 50, Max: 500},
		Rate:      domain.RateConfig{Min: 0.0001, Max: 0.001},
		Period:    domain.PeriodConfig{Min: 2, Max: 30},
		AutoRenew: false,
	}
}

func testLogger() *zap.Logger {
	log, _ := zap.NewDevelopment()
	return log
}

func TestStrategy_SubmitWhenBalanceAvailable(t *testing.T) {
	var submitted bool
	var mu sync.Mutex

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		// First call: GetFundingBalance (wallets)
		// Second call: GetActiveFundingOffers (offers)
		// Third call: SubmitFundingOffer (submit)
		if !submitted {
			// Check URL path to determine which call
			switch {
			case contains(r.URL.Path, "wallets"):
				w.Write([]byte(`[["funding","USD",1000,0,800,null,null]]`))
			case contains(r.URL.Path, "offers"):
				w.Write([]byte(`[]`))
			case contains(r.URL.Path, "submit"):
				submitted = true
				w.Write([]byte(`[1234,"fon-req",null,null,[99999,"fUSD",` + fmt.Sprintf("%d", time.Now().UnixMilli()) + `,` + fmt.Sprintf("%d", time.Now().UnixMilli()) + `,500,500,"LIMIT",null,null,null,null,null,null,null,0.0001,2,0,0,null,0,null],null,"SUCCESS",null]`))
			}
		}
	}))
	defer ts.Close()

	bfx := bitfinex.NewClientWithBaseURL(ts.Client(), ts.URL)
	ExecuteStrategy(context.Background(), bfx, "key", "secret", testConfig(), testLogger())

	mu.Lock()
	defer mu.Unlock()
	if !submitted {
		t.Error("expected offer to be submitted")
	}
}

func TestStrategy_SkipWhenLowBalance(t *testing.T) {
	submitted := false

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case contains(r.URL.Path, "wallets"):
			w.Write([]byte(`[["funding","USD",30,0,30,null,null]]`))
		case contains(r.URL.Path, "offers"):
			w.Write([]byte(`[]`))
		case contains(r.URL.Path, "submit"):
			submitted = true
			w.Write([]byte(`[1234,"fon-req",null,null,[99999,"fUSD",0,0,30,30,"LIMIT",null,null,null,null,null,null,null,0.0001,2,0,0,null,0,null],null,"SUCCESS",null]`))
		}
	}))
	defer ts.Close()

	bfx := bitfinex.NewClientWithBaseURL(ts.Client(), ts.URL)
	ExecuteStrategy(context.Background(), bfx, "key", "secret", testConfig(), testLogger())

	if submitted {
		t.Error("should not submit when balance < 50")
	}
}

func TestStrategy_SkipWhenActiveOfferExists(t *testing.T) {
	submitted := false

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case contains(r.URL.Path, "wallets"):
			w.Write([]byte(`[["funding","USD",1000,0,800,null,null]]`))
		case contains(r.URL.Path, "offers"):
			// Active offer created just now (within TTL)
			now := time.Now().UnixMilli()
			w.Write([]byte(fmt.Sprintf(`[[12345,"fUSD",%d,%d,100,100,"LIMIT",null,null,0,"ACTIVE",null,null,null,0.0001,2,0,0,null,0,null]]`, now, now)))
		case contains(r.URL.Path, "submit"):
			submitted = true
		}
	}))
	defer ts.Close()

	bfx := bitfinex.NewClientWithBaseURL(ts.Client(), ts.URL)
	ExecuteStrategy(context.Background(), bfx, "key", "secret", testConfig(), testLogger())

	if submitted {
		t.Error("should not submit when active offer exists")
	}
}

func TestStrategy_CancelStaleOffer(t *testing.T) {
	cancelled := false
	submitted := false

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case contains(r.URL.Path, "wallets"):
			w.Write([]byte(`[["funding","USD",1000,0,800,null,null]]`))
		case contains(r.URL.Path, "cancel"):
			cancelled = true
			w.Write([]byte(`[1234,"foc-req",null,null,[12345,"fUSD",0,0,100,100,"LIMIT",null,null,null,"EXECUTED",null,null,null,0.0001,2,0,0,null,0,null],null,"SUCCESS",null]`))
		case contains(r.URL.Path, "submit"):
			submitted = true
			w.Write([]byte(`[1234,"fon-req",null,null,[99999,"fUSD",0,0,500,500,"LIMIT",null,null,null,null,null,null,null,0.0001,2,0,0,null,0,null],null,"SUCCESS",null]`))
		case contains(r.URL.Path, "offers"):
			// Stale offer created 20 minutes ago
			staleTime := time.Now().Add(-20 * time.Minute).UnixMilli()
			w.Write([]byte(fmt.Sprintf(`[[12345,"fUSD",%d,%d,100,100,"LIMIT",null,null,0,"ACTIVE",null,null,null,0.0001,2,0,0,null,0,null]]`, staleTime, staleTime)))
		}
	}))
	defer ts.Close()

	bfx := bitfinex.NewClientWithBaseURL(ts.Client(), ts.URL)
	ExecuteStrategy(context.Background(), bfx, "key", "secret", testConfig(), testLogger())

	if !cancelled {
		t.Error("expected stale offer to be cancelled")
	}
	if !submitted {
		t.Error("expected new offer to be submitted after cancelling stale one")
	}
}

func contains(s, substr string) bool {
	return len(s) >= len(substr) && searchString(s, substr)
}

func searchString(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}
