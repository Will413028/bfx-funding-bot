package service

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

type mockConfigRepo struct {
	configs map[string]*storedConfig // keyed by userID
}

type storedConfig struct {
	uc *domain.UserConfig
}

func newMockConfigRepo() *mockConfigRepo {
	return &mockConfigRepo{configs: make(map[string]*storedConfig)}
}

func (m *mockConfigRepo) Upsert(_ context.Context, userID string, configJSON []byte) (*domain.UserConfig, error) {
	var cfg domain.StrategyConfig
	if err := json.Unmarshal(configJSON, &cfg); err != nil {
		return nil, err
	}

	now := time.Now()
	sc, exists := m.configs[userID]
	if exists {
		sc.uc.Config = cfg
		sc.uc.UpdatedAt = now
		return sc.uc, nil
	}

	uc := &domain.UserConfig{
		ID:        "cfg-" + userID,
		UserID:    userID,
		Config:    cfg,
		CreatedAt: now,
		UpdatedAt: now,
	}
	m.configs[userID] = &storedConfig{uc: uc}
	return uc, nil
}

func (m *mockConfigRepo) GetByUserID(_ context.Context, userID string) (*domain.UserConfig, error) {
	sc, ok := m.configs[userID]
	if !ok {
		return nil, nil
	}
	return sc.uc, nil
}

func (m *mockConfigRepo) DeleteByUserID(_ context.Context, userID string) error {
	if _, ok := m.configs[userID]; !ok {
		return domain.ErrConfigNotFound()
	}
	delete(m.configs, userID)
	return nil
}

func validConfig() domain.StrategyConfig {
	return domain.StrategyConfig{
		Currency:  "USD",
		Amount:    domain.AmountConfig{Min: 50, Max: 1000},
		Rate:      domain.RateConfig{Min: 0.0001, Max: 0.001},
		Period:    domain.PeriodConfig{Min: 2, Max: 30},
		AutoRenew: true,
	}
}

func TestConfig_Save_Success(t *testing.T) {
	svc := NewConfigService(newMockConfigRepo())

	uc, err := svc.Save(context.Background(), "user-1", validConfig())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if uc.Config.Currency != "USD" {
		t.Errorf("expected currency USD, got %s", uc.Config.Currency)
	}
	if uc.UserID != "user-1" {
		t.Errorf("expected userID user-1, got %s", uc.UserID)
	}
}

func TestConfig_Save_ValidationError(t *testing.T) {
	svc := NewConfigService(newMockConfigRepo())

	bad := validConfig()
	bad.Currency = ""
	bad.Amount.Min = 10

	_, err := svc.Save(context.Background(), "user-1", bad)
	assertAppErrorCode(t, err, "VALIDATION_ERROR")
}

func TestConfig_Save_Upsert(t *testing.T) {
	svc := NewConfigService(newMockConfigRepo())

	cfg1 := validConfig()
	cfg1.Amount.Max = 500
	_, _ = svc.Save(context.Background(), "user-1", cfg1)

	cfg2 := validConfig()
	cfg2.Amount.Max = 2000
	uc, err := svc.Save(context.Background(), "user-1", cfg2)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if uc.Config.Amount.Max != 2000 {
		t.Errorf("expected max amount 2000, got %f", uc.Config.Amount.Max)
	}
}

func TestConfig_Get_Exists(t *testing.T) {
	svc := NewConfigService(newMockConfigRepo())

	_, _ = svc.Save(context.Background(), "user-1", validConfig())

	uc, err := svc.Get(context.Background(), "user-1")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if uc.Config.Currency != "USD" {
		t.Errorf("expected currency USD, got %s", uc.Config.Currency)
	}
}

func TestConfig_Get_NotFound(t *testing.T) {
	svc := NewConfigService(newMockConfigRepo())

	_, err := svc.Get(context.Background(), "user-1")
	assertAppErrorCode(t, err, "NOT_FOUND")
}

func TestConfig_Delete_Success(t *testing.T) {
	svc := NewConfigService(newMockConfigRepo())

	_, _ = svc.Save(context.Background(), "user-1", validConfig())
	err := svc.Delete(context.Background(), "user-1")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	_, err = svc.Get(context.Background(), "user-1")
	assertAppErrorCode(t, err, "NOT_FOUND")
}

func TestConfig_Delete_NotFound(t *testing.T) {
	svc := NewConfigService(newMockConfigRepo())

	err := svc.Delete(context.Background(), "user-1")
	assertAppErrorCode(t, err, "NOT_FOUND")
}
