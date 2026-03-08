package service

import (
	"context"
	"crypto/rand"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"

	"github.com/will/bfx-funding-bot/backend/internal/crypto"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

type mockAPIKeyRepo struct {
	keys map[string]*storedKey // keyed by ID
}

type storedKey struct {
	key             *domain.APIKey
	encryptedSecret []byte
}

func newMockAPIKeyRepo() *mockAPIKeyRepo {
	return &mockAPIKeyRepo{keys: make(map[string]*storedKey)}
}

func (m *mockAPIKeyRepo) Create(_ context.Context, userID, label, apiKey string, encryptedSecret []byte) (*domain.APIKey, error) {
	// Check unique constraint on userID
	for _, sk := range m.keys {
		if sk.key.UserID == userID {
			return nil, &pgconn.PgError{Code: "23505"}
		}
	}
	k := &domain.APIKey{
		ID:        "key-" + userID,
		UserID:    userID,
		Label:     label,
		APIKey:    apiKey,
		CreatedAt: time.Now(),
		UpdatedAt: time.Now(),
	}
	m.keys[k.ID] = &storedKey{key: k, encryptedSecret: encryptedSecret}
	return k, nil
}

func (m *mockAPIKeyRepo) GetByID(_ context.Context, id string) (*domain.APIKey, []byte, error) {
	sk, ok := m.keys[id]
	if !ok {
		return nil, nil, domain.ErrAPIKeyNotFound()
	}
	return sk.key, sk.encryptedSecret, nil
}

func (m *mockAPIKeyRepo) GetByUserID(_ context.Context, userID string) (*domain.APIKey, []byte, error) {
	for _, sk := range m.keys {
		if sk.key.UserID == userID {
			return sk.key, sk.encryptedSecret, nil
		}
	}
	return nil, nil, nil
}

func (m *mockAPIKeyRepo) Delete(_ context.Context, id, userID string) error {
	sk, ok := m.keys[id]
	if !ok || sk.key.UserID != userID {
		return domain.ErrAPIKeyNotFound()
	}
	delete(m.keys, id)
	return nil
}

func testAES(t *testing.T) *crypto.AES {
	t.Helper()
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		t.Fatal(err)
	}
	a, err := crypto.NewAES(key)
	if err != nil {
		t.Fatal(err)
	}
	return a
}

func TestAPIKey_Create_Success(t *testing.T) {
	svc := NewAPIKeyService(newMockAPIKeyRepo(), testAES(t))

	key, err := svc.Create(context.Background(), "user-1", "bfx-key", "bfx-secret", "my key")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if key.APIKey != "bfx-key" {
		t.Errorf("expected api_key bfx-key, got %s", key.APIKey)
	}
	if key.Label != "my key" {
		t.Errorf("expected label 'my key', got %s", key.Label)
	}
}

func TestAPIKey_Create_Duplicate(t *testing.T) {
	svc := NewAPIKeyService(newMockAPIKeyRepo(), testAES(t))

	_, _ = svc.Create(context.Background(), "user-1", "key1", "secret1", "")
	_, err := svc.Create(context.Background(), "user-1", "key2", "secret2", "")
	assertAppErrorCode(t, err, "APIKEY_ALREADY_EXISTS")
}

func TestAPIKey_List_WithKey(t *testing.T) {
	svc := NewAPIKeyService(newMockAPIKeyRepo(), testAES(t))

	_, _ = svc.Create(context.Background(), "user-1", "key1", "secret1", "label1")

	keys, err := svc.List(context.Background(), "user-1")
	if err != nil {
		t.Fatal(err)
	}
	if len(keys) != 1 {
		t.Fatalf("expected 1 key, got %d", len(keys))
	}
}

func TestAPIKey_List_Empty(t *testing.T) {
	svc := NewAPIKeyService(newMockAPIKeyRepo(), testAES(t))

	keys, err := svc.List(context.Background(), "user-1")
	if err != nil {
		t.Fatal(err)
	}
	if len(keys) != 0 {
		t.Fatalf("expected 0 keys, got %d", len(keys))
	}
}

func TestAPIKey_GetByID_Success(t *testing.T) {
	repo := newMockAPIKeyRepo()
	svc := NewAPIKeyService(repo, testAES(t))

	created, _ := svc.Create(context.Background(), "user-1", "key1", "secret1", "")

	key, err := svc.GetByID(context.Background(), "user-1", created.ID)
	if err != nil {
		t.Fatal(err)
	}
	if key.ID != created.ID {
		t.Errorf("expected ID %s, got %s", created.ID, key.ID)
	}
}

func TestAPIKey_GetByID_WrongUser(t *testing.T) {
	svc := NewAPIKeyService(newMockAPIKeyRepo(), testAES(t))

	created, _ := svc.Create(context.Background(), "user-1", "key1", "secret1", "")

	_, err := svc.GetByID(context.Background(), "user-2", created.ID)
	assertAppErrorCode(t, err, "NOT_FOUND")
}

func TestAPIKey_Delete_Success(t *testing.T) {
	svc := NewAPIKeyService(newMockAPIKeyRepo(), testAES(t))

	created, _ := svc.Create(context.Background(), "user-1", "key1", "secret1", "")

	err := svc.Delete(context.Background(), "user-1", created.ID)
	if err != nil {
		t.Fatal(err)
	}

	keys, _ := svc.List(context.Background(), "user-1")
	if len(keys) != 0 {
		t.Fatal("expected key to be deleted")
	}
}

func TestAPIKey_Delete_WrongUser(t *testing.T) {
	svc := NewAPIKeyService(newMockAPIKeyRepo(), testAES(t))

	created, _ := svc.Create(context.Background(), "user-1", "key1", "secret1", "")

	err := svc.Delete(context.Background(), "user-2", created.ID)
	assertAppErrorCode(t, err, "NOT_FOUND")
}
