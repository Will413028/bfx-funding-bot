package lending

import (
	"context"
	"fmt"
	"testing"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/lending/worker"
)

// --- Stubs for fetcher interfaces ---

type stubFetcherClient struct {
	balance float64
	offers  []domain.FundingOffer
	credits []domain.FundingCredit
}

func (s *stubFetcherClient) GetFundingBalance(ctx context.Context, apiKey, apiSecret, currency string) (*domain.Wallet, error) {
	return &domain.Wallet{BalanceAvailable: s.balance}, nil
}

func (s *stubFetcherClient) GetActiveFundingOffers(ctx context.Context, apiKey, apiSecret, currency string) ([]domain.FundingOffer, error) {
	return s.offers, nil
}

func (s *stubFetcherClient) GetActiveFundingCredits(ctx context.Context, apiKey, apiSecret, currency string) ([]domain.FundingCredit, error) {
	return s.credits, nil
}

type stubFetcherKeyStore struct {
	key    *domain.APIKey
	secret []byte
	err    error
}

func (s *stubFetcherKeyStore) GetByUserID(ctx context.Context, userID string) (*domain.APIKey, []byte, error) {
	return s.key, s.secret, s.err
}

type stubFetcherCipher struct {
	plaintext []byte
	err       error
}

func (s *stubFetcherCipher) Decrypt(ciphertext []byte) ([]byte, error) {
	return s.plaintext, s.err
}

// --- Fetcher tests ---

func TestUserDataFetcher_Success(t *testing.T) {
	fetcher := newUserDataFetcher(
		&stubFetcherClient{
			balance: 5000,
			offers:  []domain.FundingOffer{{ID: 10}},
			credits: []domain.FundingCredit{{ID: 20}},
		},
		&stubFetcherKeyStore{
			key:    &domain.APIKey{APIKey: "key123"},
			secret: []byte("encrypted"),
		},
		&stubFetcherCipher{plaintext: []byte("secret123")},
		"USD",
	)

	data, err := fetcher.FetchUserData(context.Background(), "u1")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if data.Available != 5000 {
		t.Errorf("Available: got %f, want 5000", data.Available)
	}
	if len(data.ActiveOffers) != 1 {
		t.Errorf("ActiveOffers: got %d, want 1", len(data.ActiveOffers))
	}
	if len(data.ActiveCredits) != 1 {
		t.Errorf("ActiveCredits: got %d, want 1", len(data.ActiveCredits))
	}
}

func TestUserDataFetcher_NoAPIKey(t *testing.T) {
	fetcher := newUserDataFetcher(
		&stubFetcherClient{},
		&stubFetcherKeyStore{key: nil},
		&stubFetcherCipher{plaintext: []byte("secret")},
		"USD",
	)

	_, err := fetcher.FetchUserData(context.Background(), "u1")
	if err == nil {
		t.Fatal("expected error for missing API key")
	}
}

func TestUserDataFetcher_DecryptError(t *testing.T) {
	fetcher := newUserDataFetcher(
		&stubFetcherClient{},
		&stubFetcherKeyStore{
			key:    &domain.APIKey{APIKey: "key"},
			secret: []byte("encrypted"),
		},
		&stubFetcherCipher{err: fmt.Errorf("decrypt failed")},
		"USD",
	)

	_, err := fetcher.FetchUserData(context.Background(), "u1")
	if err == nil {
		t.Fatal("expected error on decrypt failure")
	}
}

func TestUserDataFetcher_RepoError(t *testing.T) {
	fetcher := newUserDataFetcher(
		&stubFetcherClient{},
		&stubFetcherKeyStore{err: fmt.Errorf("db error")},
		&stubFetcherCipher{plaintext: []byte("secret")},
		"USD",
	)

	_, err := fetcher.FetchUserData(context.Background(), "u1")
	if err == nil {
		t.Fatal("expected error on repo failure")
	}
}

// --- Adapter tests ---

func TestExecutorAdapter_InterfaceCheck(t *testing.T) {
	var _ worker.OfferExecutor = (*executorAdapter)(nil)
}

// --- Factory interface check ---

func TestDepsFactory_InterfaceCheck(t *testing.T) {
	var _ WorkerDepsFactory = (*DepsFactory)(nil)
}
