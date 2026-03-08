package bitfinex

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	gobreaker "github.com/sony/gobreaker/v2"
	"golang.org/x/time/rate"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const baseURL = "https://api.bitfinex.com"

type Client struct {
	httpClient      *http.Client
	baseURLOverride string // for testing only
	cb              *gobreaker.CircuitBreaker[[]byte]
	limiter         *rate.Limiter
}

func NewClient(httpClient *http.Client) *Client {
	return newClientInternal(httpClient, "")
}

func NewClientWithBaseURL(httpClient *http.Client, baseURL string) *Client {
	return newClientInternal(httpClient, baseURL)
}

func newClientInternal(httpClient *http.Client, baseOverride string) *Client {
	cb := gobreaker.NewCircuitBreaker[[]byte](gobreaker.Settings{
		Name:        "bitfinex-rest",
		MaxRequests: 3,
		Interval:    60 * time.Second,
		Timeout:     15 * time.Second,
		ReadyToTrip: func(counts gobreaker.Counts) bool {
			return counts.ConsecutiveFailures >= 5
		},
	})

	return &Client{
		httpClient:      httpClient,
		baseURLOverride: baseOverride,
		cb:              cb,
		limiter:         rate.NewLimiter(rate.Limit(1.0), 5), // 1 req/s, burst 5
	}
}

// CircuitBreakerState returns the current state of the circuit breaker.
func (c *Client) CircuitBreakerState() gobreaker.State {
	return c.cb.State()
}

func (c *Client) doAuth(ctx context.Context, apiPath, apiKey, apiSecret string, body any) ([]byte, error) {
	// Rate limiter: wait for token (respects context cancellation)
	if err := c.limiter.Wait(ctx); err != nil {
		return nil, fmt.Errorf("rate limiter: %w", err)
	}

	// Circuit breaker: wrap the actual HTTP call
	return c.cb.Execute(func() ([]byte, error) {
		return c.doHTTP(ctx, apiPath, apiKey, apiSecret, body)
	})
}

func (c *Client) doHTTP(ctx context.Context, apiPath, apiKey, apiSecret string, body any) ([]byte, error) {
	jsonBody, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshal request body: %w", err)
	}

	nonce := generateNonce()
	sig := computeSignature(apiPath, nonce, string(jsonBody), apiSecret)

	url := baseURL + "/" + apiPath
	if c.baseURLOverride != "" {
		url = c.baseURLOverride + "/" + apiPath
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(jsonBody))
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("bfx-apikey", apiKey)
	req.Header.Set("bfx-nonce", nonce)
	req.Header.Set("bfx-signature", sig)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("http request: %w", err)
	}
	defer resp.Body.Close()

	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}

	if err := parseError(data); err != nil {
		return nil, err
	}

	return data, nil
}

// parseError checks if the response is a Bitfinex error: ["error", code, message]
func parseError(data []byte) error {
	var raw []json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil // not a JSON array, let caller handle
	}
	if len(raw) < 3 {
		return nil
	}

	var errStr string
	if err := json.Unmarshal(raw[0], &errStr); err != nil || errStr != "error" {
		return nil
	}

	var code int
	json.Unmarshal(raw[1], &code)

	var msg string
	json.Unmarshal(raw[2], &msg)

	if code == 10100 || code == 10114 {
		return domain.ErrInvalidAPIKey()
	}

	return domain.ErrBitfinexAPI(code, msg)
}

func (c *Client) VerifyCredentials(ctx context.Context, apiKey, apiSecret string) error {
	_, err := c.doAuth(ctx, "v2/auth/r/wallets", apiKey, apiSecret, map[string]any{})
	return err
}

func (c *Client) GetFundingBalance(ctx context.Context, apiKey, apiSecret, currency string) (*domain.Wallet, error) {
	data, err := c.doAuth(ctx, "v2/auth/r/wallets", apiKey, apiSecret, map[string]any{})
	if err != nil {
		return nil, err
	}

	var wallets [][]json.RawMessage
	if err := json.Unmarshal(data, &wallets); err != nil {
		return nil, fmt.Errorf("parse wallets: %w", err)
	}

	for _, w := range wallets {
		if len(w) < 5 {
			continue
		}
		var wType, wCurrency string
		json.Unmarshal(w[0], &wType)
		json.Unmarshal(w[1], &wCurrency)

		if wType == "funding" && wCurrency == currency {
			var balance, available float64
			json.Unmarshal(w[2], &balance)
			json.Unmarshal(w[4], &available)
			return &domain.Wallet{
				Currency:         currency,
				Balance:          balance,
				BalanceAvailable: available,
			}, nil
		}
	}

	return &domain.Wallet{Currency: currency}, nil
}

func (c *Client) SubmitFundingOffer(ctx context.Context, apiKey, apiSecret string, params domain.OfferParams) (*domain.FundingOffer, error) {
	body := map[string]any{
		"type":   "LIMIT",
		"symbol": "f" + params.Currency,
		"amount": fmt.Sprintf("%f", params.Amount),
		"rate":   fmt.Sprintf("%f", params.Rate),
		"period": params.Period,
		"flags":  0,
	}

	data, err := c.doAuth(ctx, "v2/auth/w/funding/offer/submit", apiKey, apiSecret, body)
	if err != nil {
		return nil, err
	}

	return parseSubmitOfferResponse(data, params.Currency)
}

func parseSubmitOfferResponse(data []byte, currency string) (*domain.FundingOffer, error) {
	// Response: [MTS, TYPE, MSG_ID, null, [OFFER_ARRAY], CODE, STATUS, TEXT]
	var resp []json.RawMessage
	if err := json.Unmarshal(data, &resp); err != nil {
		return nil, fmt.Errorf("parse submit response: %w", err)
	}

	if len(resp) < 7 {
		return nil, fmt.Errorf("unexpected submit response length: %d", len(resp))
	}

	// Check STATUS field
	var status string
	json.Unmarshal(resp[6], &status)
	if status != "SUCCESS" {
		var text string
		if len(resp) > 7 {
			json.Unmarshal(resp[7], &text)
		}
		return nil, domain.ErrBitfinexAPI(0, fmt.Sprintf("submit failed: %s %s", status, text))
	}

	// Parse offer array at index 4
	var offerArr []json.RawMessage
	if err := json.Unmarshal(resp[4], &offerArr); err != nil {
		return nil, fmt.Errorf("parse offer array: %w", err)
	}

	if len(offerArr) < 16 {
		return nil, fmt.Errorf("offer array too short: %d", len(offerArr))
	}

	var id int64
	var amount, rate float64
	var period int64

	json.Unmarshal(offerArr[0], &id)
	json.Unmarshal(offerArr[4], &amount)
	json.Unmarshal(offerArr[14], &rate)
	json.Unmarshal(offerArr[15], &period)

	return &domain.FundingOffer{
		ID:       id,
		Currency: currency,
		Amount:   amount,
		Rate:     rate,
		Period:   int(period),
		Status:   "ACTIVE",
	}, nil
}

func (c *Client) CancelFundingOffer(ctx context.Context, apiKey, apiSecret string, offerID int64) error {
	body := map[string]any{"id": offerID}

	data, err := c.doAuth(ctx, "v2/auth/w/funding/offer/cancel", apiKey, apiSecret, body)
	if err != nil {
		return err
	}

	// Response: [MTS, "foc-req", null, null, [OFFER_ARRAY], null, "SUCCESS", null]
	var resp []json.RawMessage
	if err := json.Unmarshal(data, &resp); err != nil {
		return fmt.Errorf("parse cancel response: %w", err)
	}
	if len(resp) >= 7 {
		var status string
		json.Unmarshal(resp[6], &status)
		if status != "SUCCESS" {
			return domain.ErrBitfinexAPI(0, "cancel failed")
		}
	}

	return nil
}

func (c *Client) GetActiveFundingOffers(ctx context.Context, apiKey, apiSecret, currency string) ([]domain.FundingOffer, error) {
	apiPath := fmt.Sprintf("v2/auth/r/funding/offers/f%s", currency)
	data, err := c.doAuth(ctx, apiPath, apiKey, apiSecret, map[string]any{})
	if err != nil {
		return nil, err
	}

	var raw [][]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return []domain.FundingOffer{}, nil
	}

	offers := make([]domain.FundingOffer, 0, len(raw))
	for _, item := range raw {
		o, err := parseOfferItem(item, currency)
		if err != nil {
			continue
		}
		offers = append(offers, *o)
	}

	return offers, nil
}

func parseOfferItem(item []json.RawMessage, currency string) (*domain.FundingOffer, error) {
	// Index: 0=ID, 1=SYMBOL, 4=AMOUNT, 10=STATUS, 14=RATE, 15=PERIOD, 19=RENEW
	if len(item) < 16 {
		return nil, fmt.Errorf("offer item too short")
	}

	var id int64
	var amount, rate float64
	var period int64
	var status string

	json.Unmarshal(item[0], &id)
	json.Unmarshal(item[4], &amount)
	json.Unmarshal(item[10], &status)
	json.Unmarshal(item[14], &rate)
	json.Unmarshal(item[15], &period)

	var createdMs, updatedMs int64
	json.Unmarshal(item[2], &createdMs)
	json.Unmarshal(item[3], &updatedMs)

	return &domain.FundingOffer{
		ID:        id,
		Currency:  currency,
		Amount:    amount,
		Rate:      rate,
		Period:    int(period),
		Status:    status,
		CreatedAt: time.UnixMilli(createdMs),
		UpdatedAt: time.UnixMilli(updatedMs),
	}, nil
}

func (c *Client) GetActiveFundingCredits(ctx context.Context, apiKey, apiSecret, currency string) ([]domain.FundingCredit, error) {
	apiPath := fmt.Sprintf("v2/auth/r/funding/credits/f%s", currency)
	data, err := c.doAuth(ctx, apiPath, apiKey, apiSecret, map[string]any{})
	if err != nil {
		return nil, err
	}

	var raw [][]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return []domain.FundingCredit{}, nil
	}

	credits := make([]domain.FundingCredit, 0, len(raw))
	for _, item := range raw {
		cr, err := parseCreditItem(item, currency)
		if err != nil {
			continue
		}
		credits = append(credits, *cr)
	}

	return credits, nil
}

func parseCreditItem(item []json.RawMessage, currency string) (*domain.FundingCredit, error) {
	// Index: 0=ID, 5=AMOUNT, 7=STATUS, 11=RATE, 12=PERIOD, 13=MTS_OPENING, 18=RENEW
	if len(item) < 19 {
		return nil, fmt.Errorf("credit item too short")
	}

	var id int64
	var amount, rate float64
	var period int64
	var status string
	var openingMs int64
	var renew int

	json.Unmarshal(item[0], &id)
	json.Unmarshal(item[5], &amount)
	json.Unmarshal(item[7], &status)
	json.Unmarshal(item[11], &rate)
	json.Unmarshal(item[12], &period)
	json.Unmarshal(item[13], &openingMs)
	json.Unmarshal(item[18], &renew)

	return &domain.FundingCredit{
		ID:        id,
		Currency:  currency,
		Amount:    amount,
		Rate:      rate,
		Period:    int(period),
		Status:    status,
		AutoRenew: renew == 1,
		OpenedAt:  time.UnixMilli(openingMs),
	}, nil
}

func (c *Client) GetFundingEarnings(ctx context.Context, apiKey, apiSecret, currency string, start, end time.Time) ([]domain.FundingEarning, error) {
	apiPath := fmt.Sprintf("v2/auth/r/ledgers/f%s/hist", currency)
	body := map[string]any{
		"category": 28,
		"start":    start.UnixMilli(),
		"end":      end.UnixMilli(),
		"limit":    2500,
	}

	data, err := c.doAuth(ctx, apiPath, apiKey, apiSecret, body)
	if err != nil {
		return nil, err
	}

	var raw [][]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return []domain.FundingEarning{}, nil
	}

	earnings := make([]domain.FundingEarning, 0, len(raw))
	for _, item := range raw {
		e, err := parseLedgerItem(item, currency)
		if err != nil {
			continue
		}
		earnings = append(earnings, *e)
	}

	return earnings, nil
}

func parseLedgerItem(item []json.RawMessage, currency string) (*domain.FundingEarning, error) {
	// Response: [ID, CURRENCY, null, MTS, null, AMOUNT, BALANCE, null, DESCRIPTION]
	if len(item) < 9 {
		return nil, fmt.Errorf("ledger item too short")
	}

	var id int64
	var amount, balance float64
	var mts int64
	var description string

	json.Unmarshal(item[0], &id)
	json.Unmarshal(item[3], &mts)
	json.Unmarshal(item[5], &amount)
	json.Unmarshal(item[6], &balance)
	json.Unmarshal(item[8], &description)

	return &domain.FundingEarning{
		ID:          id,
		Currency:    currency,
		Amount:      amount,
		Balance:     balance,
		Description: description,
		Timestamp:   time.UnixMilli(mts),
	}, nil
}
