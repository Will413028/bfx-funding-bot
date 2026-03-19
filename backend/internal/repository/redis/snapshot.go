package redis

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const snapshotKeyPrefix = "market:snapshot:"

// SnapshotCacheRepo implements repository.SnapshotCache using Redis.
type SnapshotCacheRepo struct {
	client *redis.Client
}

func NewSnapshotCacheRepo(client *redis.Client) *SnapshotCacheRepo {
	return &SnapshotCacheRepo{client: client}
}

func snapshotKey(symbol string) string {
	return snapshotKeyPrefix + symbol
}

func (r *SnapshotCacheRepo) Set(ctx context.Context, symbol string, snapshot *domain.MarketSnapshot, ttl time.Duration) error {
	data, err := json.Marshal(snapshot)
	if err != nil {
		return fmt.Errorf("marshal snapshot: %w", err)
	}
	return r.client.Set(ctx, snapshotKey(symbol), data, ttl).Err()
}

func (r *SnapshotCacheRepo) Get(ctx context.Context, symbol string) (*domain.MarketSnapshot, error) {
	data, err := r.client.Get(ctx, snapshotKey(symbol)).Bytes()
	if errors.Is(err, redis.Nil) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("get snapshot: %w", err)
	}

	var snapshot domain.MarketSnapshot
	if err := json.Unmarshal(data, &snapshot); err != nil {
		return nil, fmt.Errorf("unmarshal snapshot: %w", err)
	}
	return &snapshot, nil
}

func (r *SnapshotCacheRepo) Delete(ctx context.Context, symbol string) error {
	return r.client.Del(ctx, snapshotKey(symbol)).Err()
}
