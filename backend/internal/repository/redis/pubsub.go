package redis

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"

	"github.com/redis/go-redis/v9"
	"go.uber.org/zap"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const snapshotChannel = "market:snapshot:updates"

// SnapshotPubSubRepo implements repository.SnapshotPubSub using Redis Pub/Sub.
type SnapshotPubSubRepo struct {
	client *redis.Client
	log    *zap.Logger
	pubsub *redis.PubSub
	cancel context.CancelFunc
	mu     sync.Mutex
}

func NewSnapshotPubSubRepo(client *redis.Client, log *zap.Logger) *SnapshotPubSubRepo {
	return &SnapshotPubSubRepo{
		client: client,
		log:    log,
	}
}

func (r *SnapshotPubSubRepo) Publish(ctx context.Context, snapshot *domain.MarketSnapshot) error {
	data, err := json.Marshal(snapshot)
	if err != nil {
		return fmt.Errorf("marshal snapshot: %w", err)
	}
	return r.client.Publish(ctx, snapshotChannel, data).Err()
}

func (r *SnapshotPubSubRepo) Subscribe(ctx context.Context) (<-chan *domain.MarketSnapshot, error) {
	pubsub := r.client.Subscribe(ctx, snapshotChannel)

	// Verify subscription is active
	if _, err := pubsub.Receive(ctx); err != nil {
		_ = pubsub.Close()
		return nil, fmt.Errorf("subscribe: %w", err)
	}

	subCtx, cancel := context.WithCancel(ctx)
	r.mu.Lock()
	r.pubsub = pubsub
	r.cancel = cancel
	r.mu.Unlock()

	ch := make(chan *domain.MarketSnapshot, 64)

	go func() {
		defer close(ch)
		msgCh := pubsub.Channel()

		for {
			select {
			case <-subCtx.Done():
				return
			case msg, ok := <-msgCh:
				if !ok {
					return
				}
				var snapshot domain.MarketSnapshot
				if err := json.Unmarshal([]byte(msg.Payload), &snapshot); err != nil {
					r.log.Warn("failed to unmarshal snapshot from pubsub",
						zap.Error(err), zap.String("payload", msg.Payload[:min(len(msg.Payload), 100)]))
					continue
				}
				select {
				case ch <- &snapshot:
				case <-subCtx.Done():
					return
				}
			}
		}
	}()

	return ch, nil
}

func (r *SnapshotPubSubRepo) Close() error {
	r.mu.Lock()
	cancel := r.cancel
	pubsub := r.pubsub
	r.cancel = nil
	r.pubsub = nil
	r.mu.Unlock()

	if cancel != nil {
		cancel()
	}
	if pubsub != nil {
		return pubsub.Close()
	}
	return nil
}
