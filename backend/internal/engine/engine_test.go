package engine

import (
	"context"
	"crypto/rand"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"go.uber.org/zap"

	"github.com/will/bfx-funding-bot/backend/internal/bitfinex"
	"github.com/will/bfx-funding-bot/backend/internal/crypto"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// mockAPIKeyRepo for engine tests
type mockAPIKeyRepo struct {
	keys    []domain.APIKey
	secrets [][]byte
}

func (m *mockAPIKeyRepo) Create(_ context.Context, _, _, _ string, _ []byte, _ string) (*domain.APIKey, error) {
	return nil, nil
}
func (m *mockAPIKeyRepo) GetByID(_ context.Context, _ string) (*domain.APIKey, []byte, error) {
	return nil, nil, nil
}
func (m *mockAPIKeyRepo) GetByUserID(_ context.Context, _ string) (*domain.APIKey, []byte, error) {
	return nil, nil, nil
}
func (m *mockAPIKeyRepo) UpdateExchangeStatus(_ context.Context, _, _ string) error { return nil }
func (m *mockAPIKeyRepo) Delete(_ context.Context, _, _ string) error               { return nil }
func (m *mockAPIKeyRepo) ListVerified(_ context.Context) ([]domain.APIKey, [][]byte, error) {
	return m.keys, m.secrets, nil
}

// mockConfigRepo for engine tests
type mockConfigRepo struct {
	configs map[string]*domain.UserConfig
}

func (m *mockConfigRepo) Upsert(_ context.Context, _ string, _ []byte) (*domain.UserConfig, error) {
	return nil, nil
}
func (m *mockConfigRepo) GetByUserID(_ context.Context, userID string) (*domain.UserConfig, error) {
	c, ok := m.configs[userID]
	if !ok {
		return nil, nil
	}
	return c, nil
}
func (m *mockConfigRepo) DeleteByUserID(_ context.Context, _ string) error { return nil }

func testCipher(t *testing.T) *crypto.AES {
	t.Helper()
	key := make([]byte, 32)
	rand.Read(key)
	c, err := crypto.NewAES(key)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestEngine_Tick_ProcessesVerifiedUsersWithConfig(t *testing.T) {
	cipher := testCipher(t)
	secret, _ := cipher.Encrypt([]byte("test-secret"))

	submitted := false
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case contains(r.URL.Path, "wallets"):
			w.Write([]byte(`[["funding","USD",1000,0,800,null,null]]`))
		case contains(r.URL.Path, "offers"):
			w.Write([]byte(`[]`))
		case contains(r.URL.Path, "submit"):
			submitted = true
			w.Write([]byte(`[1234,"fon-req",null,null,[99999,"fUSD",0,0,500,500,"LIMIT",null,null,null,null,null,null,null,0.0001,2,0,0,null,0,null],null,"SUCCESS",null]`))
		}
	}))
	defer ts.Close()

	bfx := bitfinex.NewClientWithBaseURL(ts.Client(), ts.URL)
	apiKeyRepo := &mockAPIKeyRepo{
		keys: []domain.APIKey{
			{ID: "key-1", UserID: "user-1", APIKey: "bfx-key", ExchangeStatus: "verified"},
		},
		secrets: [][]byte{secret},
	}
	configRepo := &mockConfigRepo{
		configs: map[string]*domain.UserConfig{
			"user-1": {
				UserID: "user-1",
				Config: domain.StrategyConfig{
					Currency:  "USD",
					Amount:    domain.AmountConfig{Min: 50, Max: 500},
					Rate:      domain.RateConfig{Min: 0.0001, Max: 0.001},
					Period:    domain.PeriodConfig{Min: 2, Max: 30},
					AutoRenew: false,
				},
			},
		},
	}

	log, _ := zap.NewDevelopment()
	engine := NewEngine(bfx, apiKeyRepo, configRepo, cipher, log)
	engine.tick(context.Background())

	if !submitted {
		t.Error("expected offer to be submitted for verified user with config")
	}
}

func TestEngine_Tick_SkipsUserWithoutConfig(t *testing.T) {
	cipher := testCipher(t)
	secret, _ := cipher.Encrypt([]byte("test-secret"))

	submitted := false
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if contains(r.URL.Path, "submit") {
			submitted = true
		}
		w.Write([]byte(`[]`))
	}))
	defer ts.Close()

	bfx := bitfinex.NewClientWithBaseURL(ts.Client(), ts.URL)
	apiKeyRepo := &mockAPIKeyRepo{
		keys: []domain.APIKey{
			{ID: "key-1", UserID: "user-1", APIKey: "bfx-key", ExchangeStatus: "verified"},
		},
		secrets: [][]byte{secret},
	}
	configRepo := &mockConfigRepo{configs: map[string]*domain.UserConfig{}}

	log, _ := zap.NewDevelopment()
	engine := NewEngine(bfx, apiKeyRepo, configRepo, cipher, log)
	engine.tick(context.Background())

	if submitted {
		t.Error("should not submit for user without config")
	}
}

func TestEngine_Tick_HandlesPerUserErrors(t *testing.T) {
	cipher := testCipher(t)
	goodSecret, _ := cipher.Encrypt([]byte("good-secret"))
	badSecret := []byte("invalid-encrypted-data") // will fail decryption

	submitted := false
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case contains(r.URL.Path, "wallets"):
			w.Write([]byte(`[["funding","USD",1000,0,800,null,null]]`))
		case contains(r.URL.Path, "offers"):
			w.Write([]byte(`[]`))
		case contains(r.URL.Path, "submit"):
			submitted = true
			w.Write([]byte(`[1234,"fon-req",null,null,[99999,"fUSD",0,0,500,500,"LIMIT",null,null,null,null,null,null,null,0.0001,2,0,0,null,0,null],null,"SUCCESS",null]`))
		}
	}))
	defer ts.Close()

	bfx := bitfinex.NewClientWithBaseURL(ts.Client(), ts.URL)
	cfg := domain.StrategyConfig{
		Currency: "USD",
		Amount:   domain.AmountConfig{Min: 50, Max: 500},
		Rate:     domain.RateConfig{Min: 0.0001, Max: 0.001},
		Period:   domain.PeriodConfig{Min: 2, Max: 30},
	}
	apiKeyRepo := &mockAPIKeyRepo{
		keys: []domain.APIKey{
			{ID: "key-1", UserID: "user-bad", APIKey: "bad-key", ExchangeStatus: "verified", CreatedAt: time.Now()},
			{ID: "key-2", UserID: "user-good", APIKey: "good-key", ExchangeStatus: "verified", CreatedAt: time.Now()},
		},
		secrets: [][]byte{badSecret, goodSecret},
	}
	configRepo := &mockConfigRepo{
		configs: map[string]*domain.UserConfig{
			"user-bad":  {UserID: "user-bad", Config: cfg},
			"user-good": {UserID: "user-good", Config: cfg},
		},
	}

	log, _ := zap.NewDevelopment()
	engine := NewEngine(bfx, apiKeyRepo, configRepo, cipher, log)
	engine.tick(context.Background())

	// First user should fail (bad secret), but second user should still be processed
	if !submitted {
		t.Error("expected second user's offer to be submitted despite first user's error")
	}
}
