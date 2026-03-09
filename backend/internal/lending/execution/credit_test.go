package execution

import (
	"context"
	"errors"
	"testing"
	"time"

	"golang.org/x/time/rate"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// --- Helpers ---

func defaultConfig() *domain.StrategyConfig {
	return &domain.StrategyConfig{
		Currency: "fUSD",
		Rate:     domain.RateConfig{Min: 0.0002, Max: 0.005},
		Period:   domain.PeriodConfig{Min: 2, Max: 30},
	}
}

func newTestCreditManager(client *mockFundingClient, keys *mockKeyStore, log *mockExecutionLog) *CreditManager {
	cm := NewCreditManager(client, keys, &mockCipher{}, log, rate.NewLimiter(rate.Inf, 0))
	return cm
}

func expiringCredit(now time.Time, hoursLeft float64, autoRenew bool) domain.FundingCredit {
	period := 10
	// openedAt = now - (period days - hoursLeft)
	openedAt := now.Add(-time.Duration(period)*24*time.Hour + time.Duration(hoursLeft*float64(time.Hour)))
	return domain.FundingCredit{
		ID:        1001,
		Currency:  "fUSD",
		Amount:    5000,
		Rate:      0.0003,
		Period:    period,
		Status:    "ACTIVE",
		AutoRenew: autoRenew,
		OpenedAt:  openedAt,
	}
}

// --- Tests ---

func TestCredit_AutoRenewExpiring(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	client := &mockFundingClient{}
	log := &mockExecutionLog{}
	cm := newTestCreditManager(client, defaultKeyStore(), log)
	cm.now = func() time.Time { return now }

	credits := []domain.FundingCredit{
		expiringCredit(now, 12, true), // 12h left, auto-renew
	}

	summary, err := cm.ProcessCredits(context.Background(), "user-1", credits, defaultConfig())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if summary.RenewOK != 1 {
		t.Errorf("RenewOK: got %d, want 1", summary.RenewOK)
	}
	if summary.Skipped != 0 {
		t.Errorf("Skipped: got %d, want 0", summary.Skipped)
	}
	if len(log.records) != 1 {
		t.Fatalf("records: got %d, want 1", len(log.records))
	}
	if log.records[0].Action != domain.ActionRenew {
		t.Errorf("action: got %s, want %s", log.records[0].Action, domain.ActionRenew)
	}
	if log.records[0].Status != "success" {
		t.Errorf("status: got %s, want success", log.records[0].Status)
	}
}

func TestCredit_SkipNonAutoRenew(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	client := &mockFundingClient{}
	log := &mockExecutionLog{}
	cm := newTestCreditManager(client, defaultKeyStore(), log)
	cm.now = func() time.Time { return now }

	credits := []domain.FundingCredit{
		expiringCredit(now, 12, false), // expiring but AutoRenew=false
	}

	summary, err := cm.ProcessCredits(context.Background(), "user-1", credits, defaultConfig())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if summary.RenewOK != 0 {
		t.Errorf("RenewOK: got %d, want 0", summary.RenewOK)
	}
	if summary.Skipped != 1 {
		t.Errorf("Skipped: got %d, want 1", summary.Skipped)
	}
	if len(log.records) != 0 {
		t.Errorf("records: got %d, want 0", len(log.records))
	}
}

func TestCredit_SkipNotExpiring(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	client := &mockFundingClient{}
	log := &mockExecutionLog{}
	cm := newTestCreditManager(client, defaultKeyStore(), log)
	cm.now = func() time.Time { return now }

	credits := []domain.FundingCredit{
		expiringCredit(now, 48, true), // 48h left, not expiring yet
	}

	summary, err := cm.ProcessCredits(context.Background(), "user-1", credits, defaultConfig())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if summary.RenewOK != 0 {
		t.Errorf("RenewOK: got %d, want 0", summary.RenewOK)
	}
	if summary.Skipped != 1 {
		t.Errorf("Skipped: got %d, want 1", summary.Skipped)
	}
}

func TestCredit_ConfigRateFloor(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	var submittedRate float64
	client := &mockFundingClient{
		submitFn: func(ctx context.Context, apiKey, apiSecret string, params domain.OfferParams) (*domain.FundingOffer, error) {
			submittedRate = params.Rate
			return &domain.FundingOffer{ID: 999}, nil
		},
	}
	log := &mockExecutionLog{}
	cm := newTestCreditManager(client, defaultKeyStore(), log)
	cm.now = func() time.Time { return now }

	// Credit rate 0.0001, config min 0.0003 → should use 0.0003
	credit := expiringCredit(now, 12, true)
	credit.Rate = 0.0001

	config := defaultConfig()
	config.Rate.Min = 0.0003

	summary, err := cm.ProcessCredits(context.Background(), "user-1", []domain.FundingCredit{credit}, config)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if summary.RenewOK != 1 {
		t.Errorf("RenewOK: got %d, want 1", summary.RenewOK)
	}
	if submittedRate != 0.0003 {
		t.Errorf("rate: got %f, want 0.0003", submittedRate)
	}
}

func TestCredit_ConfigRateAboveMin(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	var submittedRate float64
	client := &mockFundingClient{
		submitFn: func(ctx context.Context, apiKey, apiSecret string, params domain.OfferParams) (*domain.FundingOffer, error) {
			submittedRate = params.Rate
			return &domain.FundingOffer{ID: 999}, nil
		},
	}
	log := &mockExecutionLog{}
	cm := newTestCreditManager(client, defaultKeyStore(), log)
	cm.now = func() time.Time { return now }

	// Credit rate 0.0005 > config min 0.0003 → should use 0.0005
	credit := expiringCredit(now, 12, true)
	credit.Rate = 0.0005

	summary, err := cm.ProcessCredits(context.Background(), "user-1", []domain.FundingCredit{credit}, defaultConfig())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if summary.RenewOK != 1 {
		t.Errorf("RenewOK: got %d, want 1", summary.RenewOK)
	}
	if submittedRate != 0.0005 {
		t.Errorf("rate: got %f, want 0.0005", submittedRate)
	}
}

func TestCredit_ConfigPeriodFloor(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	var submittedPeriod int
	client := &mockFundingClient{
		submitFn: func(ctx context.Context, apiKey, apiSecret string, params domain.OfferParams) (*domain.FundingOffer, error) {
			submittedPeriod = params.Period
			return &domain.FundingOffer{ID: 999}, nil
		},
	}
	log := &mockExecutionLog{}
	cm := newTestCreditManager(client, defaultKeyStore(), log)
	cm.now = func() time.Time { return now }

	// Credit period=2, config.Period.Min=5 → should use 5
	credit := expiringCredit(now, 12, true)
	credit.Period = 2
	// Recalculate openedAt for period=2
	credit.OpenedAt = now.Add(-time.Duration(2)*24*time.Hour + 12*time.Hour)

	config := defaultConfig()
	config.Period.Min = 5

	summary, err := cm.ProcessCredits(context.Background(), "user-1", []domain.FundingCredit{credit}, config)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if summary.RenewOK != 1 {
		t.Errorf("RenewOK: got %d, want 1", summary.RenewOK)
	}
	if submittedPeriod != 5 {
		t.Errorf("period: got %d, want 5", submittedPeriod)
	}
}

func TestCredit_RenewalFailureContinues(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	callCount := 0
	client := &mockFundingClient{
		submitFn: func(ctx context.Context, apiKey, apiSecret string, params domain.OfferParams) (*domain.FundingOffer, error) {
			callCount++
			if callCount == 2 {
				return nil, errors.New("submit failed")
			}
			return &domain.FundingOffer{ID: int64(callCount)}, nil
		},
	}
	log := &mockExecutionLog{}
	cm := newTestCreditManager(client, defaultKeyStore(), log)
	cm.now = func() time.Time { return now }

	credits := []domain.FundingCredit{
		expiringCredit(now, 12, true),
		expiringCredit(now, 6, true),
		expiringCredit(now, 3, true),
	}
	// Give unique IDs
	credits[0].ID = 1001
	credits[1].ID = 1002
	credits[2].ID = 1003

	summary, err := cm.ProcessCredits(context.Background(), "user-1", credits, defaultConfig())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if summary.RenewOK != 2 {
		t.Errorf("RenewOK: got %d, want 2", summary.RenewOK)
	}
	if summary.RenewFail != 1 {
		t.Errorf("RenewFail: got %d, want 1", summary.RenewFail)
	}
	if len(log.records) != 3 {
		t.Fatalf("records: got %d, want 3", len(log.records))
	}
	if log.records[1].Status != "error" {
		t.Errorf("2nd record status: got %s, want error", log.records[1].Status)
	}
	if log.records[1].ErrorMessage == nil {
		t.Error("2nd record should have error message")
	}
}

func TestCredit_APIKeyNotFound(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	client := &mockFundingClient{}
	keys := &mockKeyStore{err: errors.New("not found")}
	log := &mockExecutionLog{}
	cm := newTestCreditManager(client, keys, log)
	cm.now = func() time.Time { return now }

	credits := []domain.FundingCredit{
		expiringCredit(now, 12, true),
	}

	_, err := cm.ProcessCredits(context.Background(), "user-1", credits, defaultConfig())
	if err == nil {
		t.Fatal("expected error for missing API key")
	}
	if len(log.records) != 0 {
		t.Errorf("records: got %d, want 0", len(log.records))
	}
}

func TestCredit_EmptyCredits(t *testing.T) {
	client := &mockFundingClient{}
	log := &mockExecutionLog{}
	cm := newTestCreditManager(client, defaultKeyStore(), log)

	summary, err := cm.ProcessCredits(context.Background(), "user-1", nil, defaultConfig())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if summary.RenewOK != 0 || summary.RenewFail != 0 || summary.Skipped != 0 {
		t.Errorf("expected zero summary, got %+v", summary)
	}
}

func TestCredit_MixedResults(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	callCount := 0
	client := &mockFundingClient{
		submitFn: func(ctx context.Context, apiKey, apiSecret string, params domain.OfferParams) (*domain.FundingOffer, error) {
			callCount++
			if callCount == 1 {
				return nil, errors.New("submit err")
			}
			return &domain.FundingOffer{ID: int64(callCount)}, nil
		},
	}
	log := &mockExecutionLog{}
	cm := newTestCreditManager(client, defaultKeyStore(), log)
	cm.now = func() time.Time { return now }

	credits := []domain.FundingCredit{
		expiringCredit(now, 12, true),  // expiring, auto-renew → fail
		expiringCredit(now, 6, true),   // expiring, auto-renew → ok
		expiringCredit(now, 48, true),  // not expiring → skip
		expiringCredit(now, 3, false),  // expiring, no auto-renew → skip
		expiringCredit(now, 20, false), // not expiring → skip
	}

	summary, err := cm.ProcessCredits(context.Background(), "user-1", credits, defaultConfig())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if summary.RenewOK != 1 {
		t.Errorf("RenewOK: got %d, want 1", summary.RenewOK)
	}
	if summary.RenewFail != 1 {
		t.Errorf("RenewFail: got %d, want 1", summary.RenewFail)
	}
	if summary.Skipped != 3 {
		t.Errorf("Skipped: got %d, want 3", summary.Skipped)
	}
	if len(log.records) != 2 {
		t.Errorf("records: got %d, want 2 (only renewals logged)", len(log.records))
	}
}

func TestCredit_DecryptFailure(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	client := &mockFundingClient{}
	log := &mockExecutionLog{}
	cm := NewCreditManager(client, defaultKeyStore(), &mockCipher{err: errors.New("decrypt failed")}, log, rate.NewLimiter(rate.Inf, 0))
	cm.now = func() time.Time { return now }

	credits := []domain.FundingCredit{
		expiringCredit(now, 12, true),
	}

	_, err := cm.ProcessCredits(context.Background(), "user-1", credits, defaultConfig())
	if err == nil {
		t.Fatal("expected error for decrypt failure")
	}
	if len(log.records) != 0 {
		t.Errorf("records: got %d, want 0", len(log.records))
	}
}
