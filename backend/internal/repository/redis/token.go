package redis

import (
	"context"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

const tokenKeyPrefix = "auth:token:" //nolint:gosec // Redis key prefix, not a credential

// TokenRepo implements repository.TokenRepository using Redis.
type TokenRepo struct {
	client *redis.Client
}

func NewTokenRepo(client *redis.Client) *TokenRepo {
	return &TokenRepo{client: client}
}

func tokenKey(tokenHash, tokenType string) string {
	return tokenKeyPrefix + tokenType + ":" + tokenHash
}

func (r *TokenRepo) Store(ctx context.Context, tokenHash string, userID string, tokenType string, ttl time.Duration) error {
	return r.client.Set(ctx, tokenKey(tokenHash, tokenType), userID, ttl).Err()
}

func (r *TokenRepo) Get(ctx context.Context, tokenHash string, tokenType string) (string, error) {
	userID, err := r.client.Get(ctx, tokenKey(tokenHash, tokenType)).Result()
	if err == redis.Nil {
		return "", fmt.Errorf("token not found")
	}
	if err != nil {
		return "", fmt.Errorf("get token: %w", err)
	}
	return userID, nil
}

func (r *TokenRepo) Delete(ctx context.Context, tokenHash string, tokenType string) error {
	return r.client.Del(ctx, tokenKey(tokenHash, tokenType)).Err()
}
