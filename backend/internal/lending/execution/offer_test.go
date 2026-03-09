package execution

import (
	"context"
	"errors"
	"testing"

	"golang.org/x/time/rate"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// --- Mock implementations ---

type mockFundingClient struct {
	submitFn func(ctx context.Context, apiKey, apiSecret string, params domain.OfferParams) (*domain.FundingOffer, error)
	cancelFn func(ctx context.Context, apiKey, apiSecret string, offerID int64) error
	calls    []string // track call order
}

func (m *mockFundingClient) SubmitFundingOffer(ctx context.Context, apiKey, apiSecret string, params domain.OfferParams) (*domain.FundingOffer, error) {
	m.calls = append(m.calls, "submit")
	if m.submitFn != nil {
		return m.submitFn(ctx, apiKey, apiSecret, params)
	}
	return &domain.FundingOffer{ID: 999, Amount: params.Amount, Rate: params.Rate, Period: params.Period}, nil
}

func (m *mockFundingClient) CancelFundingOffer(ctx context.Context, apiKey, apiSecret string, offerID int64) error {
	m.calls = append(m.calls, "cancel")
	if m.cancelFn != nil {
		return m.cancelFn(ctx, apiKey, apiSecret, offerID)
	}
	return nil
}

type mockKeyStore struct {
	apiKey    *domain.APIKey
	encSecret []byte
	err       error
}

func (m *mockKeyStore) GetByUserID(ctx context.Context, userID string) (*domain.APIKey, []byte, error) {
	return m.apiKey, m.encSecret, m.err
}

type mockCipher struct {
	decrypted []byte
	err       error
}

func (m *mockCipher) Decrypt(ciphertext []byte) ([]byte, error) {
	if m.err != nil {
		return nil, m.err
	}
	if m.decrypted != nil {
		return m.decrypted, nil
	}
	return ciphertext, nil // pass-through
}

type mockExecutionLog struct {
	records []*domain.ExecutionRecord
}

func (m *mockExecutionLog) Create(ctx context.Context, record *domain.ExecutionRecord) (*domain.ExecutionRecord, error) {
	record.ID = "rec-1"
	m.records = append(m.records, record)
	return record, nil
}

// --- Helpers ---

func defaultKeyStore() *mockKeyStore {
	return &mockKeyStore{
		apiKey:    &domain.APIKey{APIKey: "test-key"},
		encSecret: []byte("test-secret"),
	}
}

func newTestExecutor(client *mockFundingClient, keys *mockKeyStore, log *mockExecutionLog) *OfferExecutor {
	return NewOfferExecutor(client, keys, &mockCipher{}, log, rate.NewLimiter(rate.Inf, 0))
}

// --- Tests ---

func TestExecute_SuccessfulPlacement(t *testing.T) {
	client := &mockFundingClient{}
	log := &mockExecutionLog{}
	exec := newTestExecutor(client, defaultKeyStore(), log)

	decision := &domain.DecisionResult{
		Offers: []domain.OfferDecision{
			{Amount: 1000, Rate: 0.0003, Period: 5},
		},
	}

	summary, err := exec.ExecuteDecision(context.Background(), "user-1", decision)

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if summary.PlaceOK != 1 {
		t.Errorf("PlaceOK: got %d, want 1", summary.PlaceOK)
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

func TestExecute_SuccessfulCancellation(t *testing.T) {
	client := &mockFundingClient{}
	log := &mockExecutionLog{}
	exec := newTestExecutor(client, defaultKeyStore(), log)

	decision := &domain.DecisionResult{
		Cancels: []int64{101, 102},
	}

	summary, err := exec.ExecuteDecision(context.Background(), "user-1", decision)

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if summary.CancelOK != 2 {
		t.Errorf("CancelOK: got %d, want 2", summary.CancelOK)
	}
	if len(log.records) != 2 {
		t.Fatalf("records: got %d, want 2", len(log.records))
	}
	for _, r := range log.records {
		if r.Action != domain.ActionCancel {
			t.Errorf("action: got %s, want %s", r.Action, domain.ActionCancel)
		}
		if r.Status != "success" {
			t.Errorf("status: got %s, want success", r.Status)
		}
	}
}

func TestExecute_CancelFailureContinues(t *testing.T) {
	callCount := 0
	client := &mockFundingClient{
		cancelFn: func(ctx context.Context, apiKey, apiSecret string, offerID int64) error {
			callCount++
			if callCount == 1 {
				return errors.New("cancel failed")
			}
			return nil
		},
	}
	log := &mockExecutionLog{}
	exec := newTestExecutor(client, defaultKeyStore(), log)

	decision := &domain.DecisionResult{
		Cancels: []int64{101, 102},
	}

	summary, err := exec.ExecuteDecision(context.Background(), "user-1", decision)

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if summary.CancelFail != 1 {
		t.Errorf("CancelFail: got %d, want 1", summary.CancelFail)
	}
	if summary.CancelOK != 1 {
		t.Errorf("CancelOK: got %d, want 1", summary.CancelOK)
	}
	// Both should be logged
	if len(log.records) != 2 {
		t.Fatalf("records: got %d, want 2", len(log.records))
	}
	if log.records[0].Status != "error" {
		t.Errorf("first record status: got %s, want error", log.records[0].Status)
	}
	if log.records[0].ErrorMessage == nil {
		t.Error("first record should have error message")
	}
}

func TestExecute_OfferFailureContinues(t *testing.T) {
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
	exec := newTestExecutor(client, defaultKeyStore(), log)

	decision := &domain.DecisionResult{
		Offers: []domain.OfferDecision{
			{Amount: 1000, Rate: 0.0003, Period: 5},
			{Amount: 2000, Rate: 0.0004, Period: 10},
			{Amount: 3000, Rate: 0.0005, Period: 15},
		},
	}

	summary, err := exec.ExecuteDecision(context.Background(), "user-1", decision)

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if summary.PlaceOK != 2 {
		t.Errorf("PlaceOK: got %d, want 2", summary.PlaceOK)
	}
	if summary.PlaceFail != 1 {
		t.Errorf("PlaceFail: got %d, want 1", summary.PlaceFail)
	}
	if len(log.records) != 3 {
		t.Fatalf("records: got %d, want 3", len(log.records))
	}
}

func TestExecute_APIKeyNotFound(t *testing.T) {
	client := &mockFundingClient{}
	keys := &mockKeyStore{err: errors.New("not found")}
	log := &mockExecutionLog{}
	exec := newTestExecutor(client, keys, log)

	decision := &domain.DecisionResult{
		Offers: []domain.OfferDecision{
			{Amount: 1000, Rate: 0.0003, Period: 5},
		},
	}

	_, err := exec.ExecuteDecision(context.Background(), "user-1", decision)

	if err == nil {
		t.Fatal("expected error for missing API key")
	}
	if len(log.records) != 0 {
		t.Errorf("records: got %d, want 0 (no operations should execute)", len(log.records))
	}
}

func TestExecute_EmptyDecision(t *testing.T) {
	client := &mockFundingClient{}
	log := &mockExecutionLog{}
	exec := newTestExecutor(client, defaultKeyStore(), log)

	decision := &domain.DecisionResult{}

	summary, err := exec.ExecuteDecision(context.Background(), "user-1", decision)

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if summary.PlaceOK != 0 || summary.CancelOK != 0 {
		t.Errorf("expected zero summary, got %+v", summary)
	}
	if len(log.records) != 0 {
		t.Errorf("records: got %d, want 0", len(log.records))
	}
}

func TestExecute_NilDecision(t *testing.T) {
	client := &mockFundingClient{}
	log := &mockExecutionLog{}
	exec := newTestExecutor(client, defaultKeyStore(), log)

	summary, err := exec.ExecuteDecision(context.Background(), "user-1", nil)

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if summary.PlaceOK != 0 || summary.CancelOK != 0 {
		t.Errorf("expected zero summary, got %+v", summary)
	}
}

func TestExecute_CancelBeforeOffer(t *testing.T) {
	client := &mockFundingClient{}
	log := &mockExecutionLog{}
	exec := newTestExecutor(client, defaultKeyStore(), log)

	decision := &domain.DecisionResult{
		Cancels: []int64{101},
		Offers: []domain.OfferDecision{
			{Amount: 1000, Rate: 0.0003, Period: 5},
		},
	}

	_, err := exec.ExecuteDecision(context.Background(), "user-1", decision)

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// Check call order
	if len(client.calls) != 2 {
		t.Fatalf("calls: got %d, want 2", len(client.calls))
	}
	if client.calls[0] != "cancel" {
		t.Errorf("first call: got %s, want cancel", client.calls[0])
	}
	if client.calls[1] != "submit" {
		t.Errorf("second call: got %s, want submit", client.calls[1])
	}
}

func TestExecute_MixedResults(t *testing.T) {
	cancelCount := 0
	submitCount := 0
	client := &mockFundingClient{
		cancelFn: func(ctx context.Context, apiKey, apiSecret string, offerID int64) error {
			cancelCount++
			if cancelCount == 2 {
				return errors.New("cancel err")
			}
			return nil
		},
		submitFn: func(ctx context.Context, apiKey, apiSecret string, params domain.OfferParams) (*domain.FundingOffer, error) {
			submitCount++
			if submitCount == 3 {
				return nil, errors.New("submit err")
			}
			return &domain.FundingOffer{ID: int64(submitCount)}, nil
		},
	}
	log := &mockExecutionLog{}
	exec := newTestExecutor(client, defaultKeyStore(), log)

	decision := &domain.DecisionResult{
		Cancels: []int64{101, 102, 103},
		Offers: []domain.OfferDecision{
			{Amount: 1000, Rate: 0.0003, Period: 5},
			{Amount: 2000, Rate: 0.0004, Period: 10},
			{Amount: 3000, Rate: 0.0005, Period: 15},
			{Amount: 4000, Rate: 0.0006, Period: 20},
		},
	}

	summary, err := exec.ExecuteDecision(context.Background(), "user-1", decision)

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if summary.CancelOK != 2 {
		t.Errorf("CancelOK: got %d, want 2", summary.CancelOK)
	}
	if summary.CancelFail != 1 {
		t.Errorf("CancelFail: got %d, want 1", summary.CancelFail)
	}
	if summary.PlaceOK != 3 {
		t.Errorf("PlaceOK: got %d, want 3", summary.PlaceOK)
	}
	if summary.PlaceFail != 1 {
		t.Errorf("PlaceFail: got %d, want 1", summary.PlaceFail)
	}
	if len(log.records) != 7 {
		t.Errorf("records: got %d, want 7", len(log.records))
	}
}

func TestExecute_DecryptFailure(t *testing.T) {
	client := &mockFundingClient{}
	log := &mockExecutionLog{}
	exec := NewOfferExecutor(client, defaultKeyStore(), &mockCipher{err: errors.New("decrypt failed")}, log, rate.NewLimiter(rate.Inf, 0))

	decision := &domain.DecisionResult{
		Offers: []domain.OfferDecision{
			{Amount: 1000, Rate: 0.0003, Period: 5},
		},
	}

	_, err := exec.ExecuteDecision(context.Background(), "user-1", decision)

	if err == nil {
		t.Fatal("expected error for decrypt failure")
	}
	if len(log.records) != 0 {
		t.Errorf("records: got %d, want 0", len(log.records))
	}
}
