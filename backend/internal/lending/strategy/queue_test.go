package strategy

import (
	"testing"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestQueue_LowQueue(t *testing.T) {
	// rate=0.00025 (=FRR), bestAsk=0.00026 → rate < bestAsk → queueDepth=0 → ratio=0 → low
	s := NewQueueStrategy()
	ctx := testCtx()

	r := s.Apply(ctx)

	if r.Reason != "queue:low" {
		t.Fatalf("reason: got %s, want queue:low", r.Reason)
	}
	if len(r.Offers) != 1 {
		t.Fatalf("offers: got %d, want 1", len(r.Offers))
	}
	// rate unchanged (clamped negative queueDepth to 0)
	if r.Offers[0].Rate != 0.00025 {
		t.Errorf("rate: got %f, want 0.00025 (unchanged)", r.Offers[0].Rate)
	}
}

func TestQueue_LowQueueSmallRatio(t *testing.T) {
	// rate=0.00026, bestAsk=0.00026, spread=0.00002 → queueDepth=0 → low
	s := NewQueueStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.00026
	})

	r := s.Apply(ctx)

	if r.Reason != "queue:low" {
		t.Fatalf("reason: got %s, want queue:low", r.Reason)
	}
}

func TestQueue_ModerateQueue(t *testing.T) {
	// rate=0.00030, bestAsk=0.00026, spread=0.00002, askDepth=100000
	// queueDepth = 100000 × (0.00030 - 0.00026) / 0.00002 = 100000 × 2 = 200000
	// queueRatio = 200000 / 100000 = 2.0 → >50% → deep
	// Need moderate: queueRatio between 20-50%
	// queueDepth = askDepth × ratio → want ratio=0.35
	// 0.35 = (rate - 0.00026) / 0.00002 → rate - 0.00026 = 0.0000070 → rate = 0.0002670
	s := NewQueueStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.0002670
	})

	r := s.Apply(ctx)

	if r.Reason != "queue:moderate" {
		t.Fatalf("reason: got %s, want queue:moderate", r.Reason)
	}
	expectedRate := clamp(0.0002670*queueModerateDiscount, 0.0001, 0.005)
	if !approxEqual(r.Offers[0].Rate, expectedRate, 1e-8) {
		t.Errorf("rate: got %f, want %f", r.Offers[0].Rate, expectedRate)
	}
}

func TestQueue_DeepQueue(t *testing.T) {
	// rate=0.00030, bestAsk=0.00026, spread=0.00002, askDepth=100000
	// queueDepth = 100000 × (0.00030 - 0.00026) / 0.00002 = 200000
	// queueRatio = 200000 / 100000 = 2.0 → deep (>50%)
	s := NewQueueStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.00030
	})

	r := s.Apply(ctx)

	if r.Reason != "queue:deep" {
		t.Fatalf("reason: got %s, want queue:deep", r.Reason)
	}
	expectedRate := clamp(0.00030*queueDeepDiscount, 0.0001, 0.005)
	if !approxEqual(r.Offers[0].Rate, expectedRate, 1e-8) {
		t.Errorf("rate: got %f, want %f", r.Offers[0].Rate, expectedRate)
	}
}

func TestQueue_StaleOfferCancellation(t *testing.T) {
	// bestAsk=0.00026, spread=0.00002 → staleThreshold = 0.00026 + 2×0.00002 = 0.00030
	// offer rate 0.00032 > 0.00030 → stale → cancel
	// offer rate 0.00028 <= 0.00030 → keep
	s := NewQueueStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.ActiveOffers = []domain.FundingOffer{
			{ID: 101, Rate: 0.00032, Amount: 1000},
			{ID: 102, Rate: 0.00028, Amount: 2000},
		}
	})

	r := s.Apply(ctx)

	if len(r.Cancels) != 1 {
		t.Fatalf("cancels: got %d, want 1", len(r.Cancels))
	}
	if r.Cancels[0] != 101 {
		t.Errorf("cancel id: got %d, want 101", r.Cancels[0])
	}
}

func TestQueue_MultipleStaleOffers(t *testing.T) {
	s := NewQueueStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.ActiveOffers = []domain.FundingOffer{
			{ID: 201, Rate: 0.00035, Amount: 1000},
			{ID: 202, Rate: 0.00031, Amount: 2000},
			{ID: 203, Rate: 0.00025, Amount: 3000},
		}
	})

	r := s.Apply(ctx)

	if len(r.Cancels) != 2 {
		t.Fatalf("cancels: got %d, want 2", len(r.Cancels))
	}
}

func TestQueue_NoStaleOffers(t *testing.T) {
	s := NewQueueStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.ActiveOffers = []domain.FundingOffer{
			{ID: 301, Rate: 0.00028, Amount: 1000},
		}
	})

	r := s.Apply(ctx)

	if len(r.Cancels) != 0 {
		t.Errorf("cancels: got %d, want 0", len(r.Cancels))
	}
}

func TestQueue_ZeroSpread(t *testing.T) {
	s := NewQueueStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.OrderBook.Spread = 0
	})

	r := s.Apply(ctx)

	if r.Reason != "queue:no_data" {
		t.Fatalf("reason: got %s, want queue:no_data", r.Reason)
	}
}

func TestQueue_ZeroAskDepth(t *testing.T) {
	s := NewQueueStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.OrderBook.AskDepth = 0
	})

	r := s.Apply(ctx)

	if r.Reason != "queue:no_data" {
		t.Fatalf("reason: got %s, want queue:no_data", r.Reason)
	}
}

func TestQueue_FlashFreeze(t *testing.T) {
	s := NewQueueStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FlashFreeze = true
	})

	r := s.Apply(ctx)

	if r.Reason != "flash_freeze" {
		t.Fatalf("reason: got %s, want flash_freeze", r.Reason)
	}
	if len(r.Offers) != 0 {
		t.Errorf("offers: got %d, want 0", len(r.Offers))
	}
}

func TestQueue_InsufficientBalance(t *testing.T) {
	s := NewQueueStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 10
	})

	r := s.Apply(ctx)

	if r.Reason != "insufficient_balance" {
		t.Fatalf("reason: got %s, want insufficient_balance", r.Reason)
	}
	if len(r.Offers) != 0 {
		t.Errorf("offers: got %d, want 0", len(r.Offers))
	}
}

func TestQueue_RateClampedToMin(t *testing.T) {
	// Very deep queue with low FRR → discount might push below min
	s := NewQueueStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.000105
		c.Config.Rate.Min = 0.0001
		// rate=0.000105 at bestAsk=0.00026 → rate < bestAsk → queueDepth=0 → low → no discount
		// Need rate > bestAsk for queue depth > 0
		c.Snapshot.OrderBook.BestAsk = 0.0001
		c.Snapshot.OrderBook.Spread = 0.00001
		// queueDepth = 100000 × (0.000105 - 0.0001) / 0.00001 = 100000 × 0.5 = 50000
		// queueRatio = 50000/100000 = 0.5 → moderate (=50%, not >50%)
		// adjustedRate = 0.000105 × 0.98 = 0.0001029 → above min, that's fine
		// Let's make it deep instead:
		c.Snapshot.OrderBook.BestAsk = 0.00005
		c.Snapshot.OrderBook.Spread = 0.000005
		// queueDepth = 100000 × (0.000105 - 0.00005) / 0.000005 = 100000 × 11 = 1100000
		// queueRatio = 11.0 → deep
		// adjustedRate = 0.000105 × 0.95 = 0.00009975 → below min 0.0001 → clamped
	})

	r := s.Apply(ctx)

	if r.Reason != "queue:deep" {
		t.Fatalf("reason: got %s, want queue:deep", r.Reason)
	}
	if r.Offers[0].Rate != 0.0001 {
		t.Errorf("rate: got %f, want 0.0001 (clamped to min)", r.Offers[0].Rate)
	}
}
