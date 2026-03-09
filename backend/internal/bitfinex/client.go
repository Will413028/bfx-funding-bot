package bitfinex

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
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

// ClientOption configures a Client.
type ClientOption func(*Client)

// WithRateLimit sets the platform-level rate limiter.
func WithRateLimit(ratePerSec float64, burst int) ClientOption {
	return func(c *Client) {
		c.limiter = rate.NewLimiter(rate.Limit(ratePerSec), burst)
	}
}

func NewClient(httpClient *http.Client, opts ...ClientOption) *Client {
	return newClientInternal(httpClient, "", opts...)
}

func NewClientWithBaseURL(httpClient *http.Client, baseURL string, opts ...ClientOption) *Client {
	return newClientInternal(httpClient, baseURL, opts...)
}

func newClientInternal(httpClient *http.Client, baseOverride string, opts ...ClientOption) *Client {
	cb := gobreaker.NewCircuitBreaker[[]byte](gobreaker.Settings{
		Name:        "bitfinex-rest",
		MaxRequests: 3,
		Interval:    60 * time.Second,
		Timeout:     15 * time.Second,
		ReadyToTrip: func(counts gobreaker.Counts) bool {
			return counts.ConsecutiveFailures >= 5
		},
	})

	c := &Client{
		httpClient:      httpClient,
		baseURLOverride: baseOverride,
		cb:              cb,
		limiter:         rate.NewLimiter(rate.Limit(15), 20), // platform-level: 15 req/s, burst 20
	}

	for _, opt := range opts {
		opt(c)
	}

	return c
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
		if err := json.Unmarshal(w[0], &wType); err != nil {
			continue
		}
		if err := json.Unmarshal(w[1], &wCurrency); err != nil {
			continue
		}

		if wType == "funding" && wCurrency == currency {
			var balance, available float64
			if err := json.Unmarshal(w[2], &balance); err != nil {
				return nil, fmt.Errorf("parse wallet balance: %w", err)
			}
			if err := json.Unmarshal(w[4], &available); err != nil {
				return nil, fmt.Errorf("parse wallet available: %w", err)
			}
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
		"amount": strconv.FormatFloat(params.Amount, 'f', -1, 64),
		"rate":   strconv.FormatFloat(params.Rate, 'f', -1, 64),
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
	if err := json.Unmarshal(resp[6], &status); err != nil {
		return nil, fmt.Errorf("parse submit status: %w", err)
	}
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

	if err := json.Unmarshal(offerArr[0], &id); err != nil {
		return nil, fmt.Errorf("parse offer id: %w", err)
	}
	if err := json.Unmarshal(offerArr[4], &amount); err != nil {
		return nil, fmt.Errorf("parse offer amount: %w", err)
	}
	if err := json.Unmarshal(offerArr[14], &rate); err != nil {
		return nil, fmt.Errorf("parse offer rate: %w", err)
	}
	if err := json.Unmarshal(offerArr[15], &period); err != nil {
		return nil, fmt.Errorf("parse offer period: %w", err)
	}

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
		if err := json.Unmarshal(resp[6], &status); err != nil {
			return fmt.Errorf("parse cancel status: %w", err)
		}
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
		return nil, fmt.Errorf("parse offers list: %w", err)
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

	if err := json.Unmarshal(item[0], &id); err != nil {
		return nil, fmt.Errorf("parse offer id: %w", err)
	}
	if err := json.Unmarshal(item[4], &amount); err != nil {
		return nil, fmt.Errorf("parse offer amount: %w", err)
	}
	if err := json.Unmarshal(item[10], &status); err != nil {
		return nil, fmt.Errorf("parse offer status: %w", err)
	}
	if err := json.Unmarshal(item[14], &rate); err != nil {
		return nil, fmt.Errorf("parse offer rate: %w", err)
	}
	if err := json.Unmarshal(item[15], &period); err != nil {
		return nil, fmt.Errorf("parse offer period: %w", err)
	}

	var createdMs, updatedMs int64
	if err := json.Unmarshal(item[2], &createdMs); err != nil {
		return nil, fmt.Errorf("parse offer createdAt: %w", err)
	}
	if err := json.Unmarshal(item[3], &updatedMs); err != nil {
		return nil, fmt.Errorf("parse offer updatedAt: %w", err)
	}

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
		return nil, fmt.Errorf("parse credits list: %w", err)
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

	if err := json.Unmarshal(item[0], &id); err != nil {
		return nil, fmt.Errorf("parse credit id: %w", err)
	}
	if err := json.Unmarshal(item[5], &amount); err != nil {
		return nil, fmt.Errorf("parse credit amount: %w", err)
	}
	if err := json.Unmarshal(item[7], &status); err != nil {
		return nil, fmt.Errorf("parse credit status: %w", err)
	}
	if err := json.Unmarshal(item[11], &rate); err != nil {
		return nil, fmt.Errorf("parse credit rate: %w", err)
	}
	if err := json.Unmarshal(item[12], &period); err != nil {
		return nil, fmt.Errorf("parse credit period: %w", err)
	}
	if err := json.Unmarshal(item[13], &openingMs); err != nil {
		return nil, fmt.Errorf("parse credit openedAt: %w", err)
	}
	if err := json.Unmarshal(item[18], &renew); err != nil {
		return nil, fmt.Errorf("parse credit renew: %w", err)
	}

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
		return nil, fmt.Errorf("parse earnings list: %w", err)
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

	if err := json.Unmarshal(item[0], &id); err != nil {
		return nil, fmt.Errorf("parse ledger id: %w", err)
	}
	if err := json.Unmarshal(item[3], &mts); err != nil {
		return nil, fmt.Errorf("parse ledger timestamp: %w", err)
	}
	if err := json.Unmarshal(item[5], &amount); err != nil {
		return nil, fmt.Errorf("parse ledger amount: %w", err)
	}
	if err := json.Unmarshal(item[6], &balance); err != nil {
		return nil, fmt.Errorf("parse ledger balance: %w", err)
	}
	if err := json.Unmarshal(item[8], &description); err != nil {
		return nil, fmt.Errorf("parse ledger description: %w", err)
	}

	return &domain.FundingEarning{
		ID:          id,
		Currency:    currency,
		Amount:      amount,
		Balance:     balance,
		Description: description,
		Timestamp:   time.UnixMilli(mts),
	}, nil
}
