package auth

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	wsTokenTTL    = 30 * time.Second
	wsTokenPrefix = "ws:token:"
)

// WSTokenManager handles short-lived WebSocket authentication tokens.
type WSTokenManager struct {
	client *redis.Client
}

func NewWSTokenManager(client *redis.Client) *WSTokenManager {
	return &WSTokenManager{client: client}
}

// Generate creates a single-use WS token for the given user, stored in Redis with 30s TTL.
func (m *WSTokenManager) Generate(ctx context.Context, userID string) (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", fmt.Errorf("generate ws token: %w", err)
	}

	token := hex.EncodeToString(b)
	key := wsTokenPrefix + token

	if err := m.client.Set(ctx, key, userID, wsTokenTTL).Err(); err != nil {
		return "", fmt.Errorf("store ws token: %w", err)
	}

	return token, nil
}

// maxTokenLen is the expected hex-encoded length of a 32-byte token (64 chars).
const maxTokenLen = 64

// Validate checks a WS token and returns the associated user ID.
// The token is consumed (deleted) on successful validation.
func (m *WSTokenManager) Validate(ctx context.Context, token string) (string, error) {
	if len(token) == 0 || len(token) > maxTokenLen {
		return "", fmt.Errorf("ws token invalid or expired")
	}
	key := wsTokenPrefix + token

	userID, err := m.client.GetDel(ctx, key).Result()
	if err != nil {
		if err == redis.Nil {
			return "", fmt.Errorf("ws token invalid or expired")
		}
		return "", fmt.Errorf("validate ws token: %w", err)
	}

	return userID, nil
}
