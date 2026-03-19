package redis

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	perfKeyPrefix = "perf:"
	perfTTL       = 7 * 24 * time.Hour
)

// PerformanceRepo stores and retrieves PerformanceRecords in Redis.
type PerformanceRepo struct {
	client *redis.Client
}

func NewPerformanceRepo(client *redis.Client) *PerformanceRepo {
	return &PerformanceRepo{client: client}
}

func perfKey(userID string, ts time.Time) string {
	return perfKeyPrefix + userID + ":" + fmt.Sprintf("%d", ts.UnixNano())
}

func perfScanPattern(userID string) string {
	return perfKeyPrefix + userID + ":*"
}

// Store saves a PerformanceRecord to Redis with 7-day TTL.
func (r *PerformanceRepo) Store(ctx context.Context, record *domain.PerformanceRecord) error {
	data, err := json.Marshal(record)
	if err != nil {
		return fmt.Errorf("marshal performance record: %w", err)
	}
	return r.client.Set(ctx, perfKey(record.UserID, record.Timestamp), data, perfTTL).Err()
}

// ListByUser retrieves all PerformanceRecords for a user (within Redis TTL window).
func (r *PerformanceRepo) ListByUser(ctx context.Context, userID string) ([]domain.PerformanceRecord, error) {
	pattern := perfScanPattern(userID)
	var records []domain.PerformanceRecord

	iter := r.client.Scan(ctx, 0, pattern, 0).Iterator()
	for iter.Next(ctx) {
		data, err := r.client.Get(ctx, iter.Val()).Bytes()
		if err != nil {
			continue
		}
		var rec domain.PerformanceRecord
		if err := json.Unmarshal(data, &rec); err != nil {
			continue
		}
		records = append(records, rec)
	}

	return records, iter.Err()
}
