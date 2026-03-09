package execution

import (
	"context"
	"errors"
	"testing"

	"golang.org/x/time/rate"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func reinvestConfig() *domain.StrategyConfig {
	return &domain.StrategyConfig{
		Currency: "fUSD",
		Amount:   domain.AmountConfig{Min: 50, Max: 10000},
		Rate:     domain.RateConfig{Min: 0.0002, Max: 0.005},
		Period:   domain.PeriodConfig{Min: 2, Max: 30},
	}
}

func newTestCollector(client *mockFundingClient, keys *mockKeyStore, log *mockExecutionLog) *InterestCollector {
	return NewInterestCollector(client, keys, &mockCipher{}, log, rate.NewLimiter(rate.Inf, 0))
}

func TestReinvest_AboveMinimum(t *testing.T) {
	client := &mockFundingClient{}
	log := &mockExecutionLog{}
	ic := newTestCollector(client, defaultKeyStore(), log)

	summary, err := ic.CheckAndReinvest(context.Background(), "user-1", 200, reinvestConfig())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !summary.Placed {
		t.Error("expected Placed=true")
	}
	if summary.Amount != 200 {
		t.Errorf("amount: got %f, want 200", summary.Amount)
	}
	if len(log.records) != 1 {
		t.Fatalf("records: got %d, want 1", len(log.records))
	}
	if log.records[0].Action != domain.ActionPlace {
		t.Errorf("action: got %s, want %s", log.records[0].Action, domain.ActionPlace)
	}
	if log.records[0].Status != "success" {
		t.Errorf("status: got %s, want success", log.records[0].Status)
	}
}

func TestReinvest_BelowMinimum(t *testing.T) {
	client := &mockFundingClient{}
	log := &mockExecutionLog{}
	ic := newTestCollector(client, defaultKeyStore(), log)

	summary, err := ic.CheckAndReinvest(context.Background(), "user-1", 30, reinvestConfig())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if summary.Placed {
		t.Error("expected Placed=false")
	}
	if summary.Reason != "below_minimum" {
		t.Errorf("reason: got %s, want below_minimum", summary.Reason)
	}
	if len(log.records) != 0 {
		t.Errorf("records: got %d, want 0", len(log.records))
	}
}

func TestReinvest_ExactMinimum(t *testing.T) {
	client := &mockFundingClient{}
	log := &mockExecutionLog{}
	ic := newTestCollector(client, defaultKeyStore(), log)

	summary, err := ic.CheckAndReinvest(context.Background(), "user-1", 50, reinvestConfig())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !summary.Placed {
		t.Error("expected Placed=true at exact minimum")
	}
	if summary.Amount != 50 {
		t.Errorf("amount: got %f, want 50", summary.Amount)
	}
}

func TestReinvest_CappedAtMax(t *testing.T) {
	var submittedAmount float64
	client := &mockFundingClient{
		submitFn: func(ctx context.Context, apiKey, apiSecret string, params domain.OfferParams) (*domain.FundingOffer, error) {
			submittedAmount = params.Amount
			return &domain.FundingOffer{ID: 999}, nil
		},
	}
	log := &mockExecutionLog{}
	ic := newTestCollector(client, defaultKeyStore(), log)

	summary, err := ic.CheckAndReinvest(context.Background(), "user-1", 15000, reinvestConfig())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !summary.Placed {
		t.Error("expected Placed=true")
	}
	if summary.Amount != 10000 {
		t.Errorf("summary amount: got %f, want 10000", summary.Amount)
	}
	if submittedAmount != 10000 {
		t.Errorf("submitted amount: got %f, want 10000", submittedAmount)
	}
}

func TestReinvest_WithinMax(t *testing.T) {
	var submittedAmount float64
	client := &mockFundingClient{
		submitFn: func(ctx context.Context, apiKey, apiSecret string, params domain.OfferParams) (*domain.FundingOffer, error) {
			submittedAmount = params.Amount
			return &domain.FundingOffer{ID: 999}, nil
		},
	}
	log := &mockExecutionLog{}
	ic := newTestCollector(client, defaultKeyStore(), log)

	summary, err := ic.CheckAndReinvest(context.Background(), "user-1", 3000, reinvestConfig())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if submittedAmount != 3000 {
		t.Errorf("submitted amount: got %f, want 3000", submittedAmount)
	}
	if summary.Amount != 3000 {
		t.Errorf("summary amount: got %f, want 3000", summary.Amount)
	}
}

func TestReinvest_UsesConfigMinimums(t *testing.T) {
	var submittedRate float64
	var submittedPeriod int
	client := &mockFundingClient{
		submitFn: func(ctx context.Context, apiKey, apiSecret string, params domain.OfferParams) (*domain.FundingOffer, error) {
			submittedRate = params.Rate
			submittedPeriod = params.Period
			return &domain.FundingOffer{ID: 999}, nil
		},
	}
	log := &mockExecutionLog{}
	ic := newTestCollector(client, defaultKeyStore(), log)

	config := reinvestConfig()
	config.Rate.Min = 0.0003
	config.Period.Min = 5

	_, err := ic.CheckAndReinvest(context.Background(), "user-1", 1000, config)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if submittedRate != 0.0003 {
		t.Errorf("rate: got %f, want 0.0003", submittedRate)
	}
	if submittedPeriod != 5 {
		t.Errorf("period: got %d, want 5", submittedPeriod)
	}
}

func TestReinvest_SubmitFailure(t *testing.T) {
	client := &mockFundingClient{
		submitFn: func(ctx context.Context, apiKey, apiSecret string, params domain.OfferParams) (*domain.FundingOffer, error) {
			return nil, errors.New("api error")
		},
	}
	log := &mockExecutionLog{}
	ic := newTestCollector(client, defaultKeyStore(), log)

	summary, err := ic.CheckAndReinvest(context.Background(), "user-1", 1000, reinvestConfig())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if summary.Placed {
		t.Error("expected Placed=false on submit failure")
	}
	if summary.Reason != "submit_failed" {
		t.Errorf("reason: got %s, want submit_failed", summary.Reason)
	}
	if len(log.records) != 1 {
		t.Fatalf("records: got %d, want 1", len(log.records))
	}
	if log.records[0].Status != "error" {
		t.Errorf("status: got %s, want error", log.records[0].Status)
	}
	if log.records[0].ErrorMessage == nil {
		t.Error("expected error message")
	}
}

func TestReinvest_APIKeyNotFound(t *testing.T) {
	client := &mockFundingClient{}
	keys := &mockKeyStore{err: errors.New("not found")}
	log := &mockExecutionLog{}
	ic := newTestCollector(client, keys, log)

	_, err := ic.CheckAndReinvest(context.Background(), "user-1", 1000, reinvestConfig())
	if err == nil {
		t.Fatal("expected error for missing API key")
	}
	if len(log.records) != 0 {
		t.Errorf("records: got %d, want 0", len(log.records))
	}
}

func TestReinvest_DecryptFailure(t *testing.T) {
	client := &mockFundingClient{}
	log := &mockExecutionLog{}
	ic := NewInterestCollector(client, defaultKeyStore(), &mockCipher{err: errors.New("decrypt failed")}, log, rate.NewLimiter(rate.Inf, 0))

	_, err := ic.CheckAndReinvest(context.Background(), "user-1", 1000, reinvestConfig())
	if err == nil {
		t.Fatal("expected error for decrypt failure")
	}
	if len(log.records) != 0 {
		t.Errorf("records: got %d, want 0", len(log.records))
	}
}

func TestReinvest_ZeroAvailable(t *testing.T) {
	client := &mockFundingClient{}
	log := &mockExecutionLog{}
	ic := newTestCollector(client, defaultKeyStore(), log)

	summary, err := ic.CheckAndReinvest(context.Background(), "user-1", 0, reinvestConfig())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if summary.Placed {
		t.Error("expected Placed=false for zero available")
	}
	if summary.Reason != "below_minimum" {
		t.Errorf("reason: got %s, want below_minimum", summary.Reason)
	}
}
