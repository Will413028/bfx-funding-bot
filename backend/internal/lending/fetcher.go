package lending

import (
	"context"
	"fmt"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/lending/worker"
)

// fetcherClient abstracts the Bitfinex REST methods needed by the fetcher.
type fetcherClient interface {
	GetFundingBalance(ctx context.Context, apiKey, apiSecret, currency string) (*domain.Wallet, error)
	GetActiveFundingOffers(ctx context.Context, apiKey, apiSecret, currency string) ([]domain.FundingOffer, error)
	GetActiveFundingCredits(ctx context.Context, apiKey, apiSecret, currency string) ([]domain.FundingCredit, error)
}

// fetcherKeyStore retrieves the user's API key and encrypted secret.
type fetcherKeyStore interface {
	GetByUserID(ctx context.Context, userID string) (*domain.APIKey, []byte, error)
}

// fetcherCipher decrypts API key secrets.
type fetcherCipher interface {
	Decrypt(ciphertext []byte) ([]byte, error)
}

// userDataFetcher implements worker.DataFetcher by calling Bitfinex REST API.
type userDataFetcher struct {
	client   fetcherClient
	keys     fetcherKeyStore
	cipher   fetcherCipher
	currency string
}

func newUserDataFetcher(client fetcherClient, keys fetcherKeyStore, cipher fetcherCipher, currency string) *userDataFetcher {
	return &userDataFetcher{
		client:   client,
		keys:     keys,
		cipher:   cipher,
		currency: currency,
	}
}

// FetchUserData retrieves wallet balance, active offers, and active credits.
func (f *userDataFetcher) FetchUserData(ctx context.Context, userID string) (*worker.UserData, error) {
	apiKey, encSecret, err := f.keys.GetByUserID(ctx, userID)
	if err != nil {
		return nil, fmt.Errorf("get api key: %w", err)
	}
	if apiKey == nil {
		return nil, fmt.Errorf("no api key for user %s", userID)
	}

	secret, err := f.cipher.Decrypt(encSecret)
	if err != nil {
		return nil, fmt.Errorf("decrypt api secret: %w", err)
	}

	key := apiKey.APIKey
	sec := string(secret)

	wallet, err := f.client.GetFundingBalance(ctx, key, sec, f.currency)
	if err != nil {
		return nil, fmt.Errorf("get funding balance: %w", err)
	}

	offers, err := f.client.GetActiveFundingOffers(ctx, key, sec, f.currency)
	if err != nil {
		return nil, fmt.Errorf("get active offers: %w", err)
	}

	credits, err := f.client.GetActiveFundingCredits(ctx, key, sec, f.currency)
	if err != nil {
		return nil, fmt.Errorf("get active credits: %w", err)
	}

	available := 0.0
	if wallet != nil {
		available = wallet.BalanceAvailable
	}

	return &worker.UserData{
		Available:     available,
		ActiveOffers:  offers,
		ActiveCredits: credits,
	}, nil
}

// Compile-time check.
var _ worker.DataFetcher = (*userDataFetcher)(nil)
