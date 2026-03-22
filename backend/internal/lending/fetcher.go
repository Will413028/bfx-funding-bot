package lending

import (
	"context"
	"fmt"
	"sync"

	"golang.org/x/time/rate"

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
	limiter  *rate.Limiter
	currency string
}

func newUserDataFetcher(client fetcherClient, keys fetcherKeyStore, cipher fetcherCipher, currency string, limiter *rate.Limiter) *userDataFetcher {
	return &userDataFetcher{
		client:   client,
		keys:     keys,
		cipher:   cipher,
		currency: currency,
		limiter:  limiter,
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

	var (
		wallet   *domain.Wallet
		offers   []domain.FundingOffer
		credits  []domain.FundingCredit
		mu       sync.Mutex
		wg       sync.WaitGroup
		firstErr error
	)

	wg.Add(3)

	go func() {
		defer wg.Done()
		if err := f.limiter.Wait(ctx); err != nil {
			mu.Lock()
			defer mu.Unlock()
			if firstErr == nil {
				firstErr = fmt.Errorf("rate limiter: %w", err)
			}
			return
		}
		w, err := f.client.GetFundingBalance(ctx, key, sec, f.currency)
		mu.Lock()
		defer mu.Unlock()
		if err != nil {
			if firstErr == nil {
				firstErr = fmt.Errorf("get funding balance: %w", err)
			}
		} else {
			wallet = w
		}
	}()

	go func() {
		defer wg.Done()
		if err := f.limiter.Wait(ctx); err != nil {
			mu.Lock()
			defer mu.Unlock()
			if firstErr == nil {
				firstErr = fmt.Errorf("rate limiter: %w", err)
			}
			return
		}
		o, err := f.client.GetActiveFundingOffers(ctx, key, sec, f.currency)
		mu.Lock()
		defer mu.Unlock()
		if err != nil {
			if firstErr == nil {
				firstErr = fmt.Errorf("get active offers: %w", err)
			}
		} else {
			offers = o
		}
	}()

	go func() {
		defer wg.Done()
		if err := f.limiter.Wait(ctx); err != nil {
			mu.Lock()
			defer mu.Unlock()
			if firstErr == nil {
				firstErr = fmt.Errorf("rate limiter: %w", err)
			}
			return
		}
		c, err := f.client.GetActiveFundingCredits(ctx, key, sec, f.currency)
		mu.Lock()
		defer mu.Unlock()
		if err != nil {
			if firstErr == nil {
				firstErr = fmt.Errorf("get active credits: %w", err)
			}
		} else {
			credits = c
		}
	}()

	wg.Wait()

	if firstErr != nil {
		return nil, firstErr
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
