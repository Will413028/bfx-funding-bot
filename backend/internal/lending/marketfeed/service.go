package marketfeed

import (
	"context"
	"strconv"
	"sync"
	"time"

	"go.uber.org/zap"

	"github.com/will/bfx-funding-bot/backend/internal/bitfinex"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/lending/orderbook"
	"github.com/will/bfx-funding-bot/backend/internal/lending/signal"
	"github.com/will/bfx-funding-bot/backend/internal/repository"
)

const (
	defaultSnapshotInterval = 3 * time.Second
	tradeBufferDuration     = 5 * time.Minute
	snapshotCacheTTL        = 60 * time.Second
	snapshotChannelBuffer   = 16
)

// Config holds configuration for the Market Feed Service.
type Config struct {
	BookPrecision    string
	Symbols          []string
	SnapshotInterval time.Duration
	BookLength       int
}

// symbolState holds accumulated market data for a single symbol.
type symbolState struct {
	ticker       *domain.FundingTicker
	book         map[string]domain.BookEntry
	recentTrades []domain.FundingTradeRecord
	mu           sync.RWMutex
}

// Service is the Market Feed Service that receives WebSocket data,
// computes signals, and broadcasts MarketSnapshot.
type Service struct {
	cache         repository.SnapshotCache
	pubsub        repository.SnapshotPubSub
	mdcAgg        *signal.MDCAggregator
	wsClient      *bitfinex.WSClient
	log           *zap.Logger
	states        map[string]*symbolState
	flashCrash    *FlashCrashDetector
	healthTracker *signal.SignalHealthTracker
	hiddenEst     map[string]*orderbook.HiddenRatioEstimator
	compDet       map[string]*orderbook.CompetitorDetector
	regimeDet     map[string]*signal.RegimeDetector
	outCh         chan *domain.MarketSnapshot
	stopFn        context.CancelFunc
	signalSources []SignalSource
	cfg           Config
}

// NewService creates a new Market Feed Service.
func NewService(
	log *zap.Logger,
	cfg Config,
	cache repository.SnapshotCache,
	pubsub repository.SnapshotPubSub,
	signalSources []SignalSource,
	flashCrashCfg *FlashCrashConfig,
) *Service {
	if cfg.SnapshotInterval <= 0 {
		cfg.SnapshotInterval = defaultSnapshotInterval
	}
	if cfg.BookPrecision == "" {
		cfg.BookPrecision = "P0"
	}
	if cfg.BookLength <= 0 {
		cfg.BookLength = 25
	}

	states := make(map[string]*symbolState, len(cfg.Symbols))
	hiddenEst := make(map[string]*orderbook.HiddenRatioEstimator, len(cfg.Symbols))
	compDet := make(map[string]*orderbook.CompetitorDetector, len(cfg.Symbols))
	regimeDet := make(map[string]*signal.RegimeDetector, len(cfg.Symbols))
	for _, sym := range cfg.Symbols {
		states[sym] = &symbolState{
			book: make(map[string]domain.BookEntry),
		}
		hiddenEst[sym] = orderbook.NewHiddenRatioEstimator()
		compDet[sym] = orderbook.NewCompetitorDetector()
		regimeDet[sym] = signal.NewRegimeDetector(nil)
	}

	healthTracker := signal.NewSignalHealthTracker([]domain.SignalType{
		domain.SignalBookConsumption,
		domain.SignalLiquidationCascade,
		domain.SignalMarginUsage,
		domain.SignalMomentum,
		domain.SignalCrossCurrency,
		domain.SignalIntraday,
	})

	return &Service{
		log:           log,
		cfg:           cfg,
		cache:         cache,
		pubsub:        pubsub,
		signalSources: signalSources,
		flashCrash:    NewFlashCrashDetector(flashCrashCfg),
		mdcAgg:        signal.NewMDCAggregator(),
		healthTracker: healthTracker,
		states:        states,
		hiddenEst:     hiddenEst,
		compDet:       compDet,
		regimeDet:     regimeDet,
		outCh:         make(chan *domain.MarketSnapshot, snapshotChannelBuffer),
	}
}

// Snapshots returns a read-only channel that receives assembled MarketSnapshot.
func (s *Service) Snapshots() <-chan *domain.MarketSnapshot {
	return s.outCh
}

// Start connects the WebSocket, subscribes to channels, and begins the snapshot loop.
func (s *Service) Start(ctx context.Context) error {
	ctx, cancel := context.WithCancel(ctx)
	s.stopFn = cancel

	handlers := bitfinex.EventHandlers{
		OnConnect: func() {
			s.log.Info("market feed connected")
		},
		OnDisconnect: func(err error) {
			s.log.Warn("market feed disconnected", zap.Error(err))
		},
		OnError: func(err error) {
			s.log.Error("market feed ws error", zap.Error(err))
		},
		OnTicker:        s.handleTicker,
		OnBookSnapshot:  s.handleBookSnapshot,
		OnBookUpdate:    s.handleBookUpdate,
		OnTradeSnapshot: s.handleTradeSnapshot,
		OnTradeExecuted: s.handleTradeExecuted,
	}

	s.wsClient = bitfinex.NewWSClient(s.log, "", "", handlers)

	if err := s.wsClient.Connect(); err != nil {
		cancel()
		return err
	}

	// Subscribe to all symbols
	for _, sym := range s.cfg.Symbols {
		_ = s.wsClient.SubscribeTicker(sym)
		_ = s.wsClient.SubscribeBook(sym, s.cfg.BookPrecision, s.cfg.BookLength)
		_ = s.wsClient.SubscribeTrades(sym)
	}

	go s.snapshotLoop(ctx)

	return nil
}

// Stop gracefully shuts down the service.
func (s *Service) Stop() {
	if s.stopFn != nil {
		s.stopFn()
	}
	if s.wsClient != nil {
		s.wsClient.Close()
	}
	// outCh is closed by snapshotLoop when it exits (via defer),
	// ensuring no write-after-close race.
}

// --- WS callback handlers ---

func (s *Service) handleTicker(t bitfinex.FundingTicker) {
	st, ok := s.states[t.Symbol]
	if !ok {
		return
	}
	st.mu.Lock()
	st.ticker = &domain.FundingTicker{
		FRR:             t.FRR,
		Bid:             t.Bid,
		BidPeriod:       t.BidPeriod,
		BidSize:         t.BidSize,
		Ask:             t.Ask,
		AskPeriod:       t.AskPeriod,
		AskSize:         t.AskSize,
		DailyChange:     t.DailyChange,
		DailyChangePerc: t.DailyChangePerc,
		LastPrice:       t.LastPrice,
		Volume:          t.Volume,
		High:            t.High,
		Low:             t.Low,
		FRRAmountAvail:  t.FRRAmountAvail,
	}
	st.mu.Unlock()
}

func (s *Service) handleBookSnapshot(symbol string, entries []bitfinex.BookEntry) {
	st, ok := s.states[symbol]
	if !ok {
		return
	}
	st.mu.Lock()
	st.book = make(map[string]domain.BookEntry, len(entries))
	for _, e := range entries {
		key := bookEntryKey(e.Rate, e.Period)
		st.book[key] = domain.BookEntry{
			Rate:   e.Rate,
			Period: e.Period,
			Count:  e.Count,
			Amount: e.Amount,
		}
	}
	st.mu.Unlock()
}

func (s *Service) handleBookUpdate(symbol string, entry bitfinex.BookEntry) {
	st, ok := s.states[symbol]
	if !ok {
		return
	}
	st.mu.Lock()
	key := bookEntryKey(entry.Rate, entry.Period)
	if entry.Count == 0 {
		delete(st.book, key)
	} else {
		st.book[key] = domain.BookEntry{
			Rate:   entry.Rate,
			Period: entry.Period,
			Count:  entry.Count,
			Amount: entry.Amount,
		}
	}
	st.mu.Unlock()
}

func (s *Service) handleTradeSnapshot(symbol string, trades []bitfinex.FundingTrade) {
	st, ok := s.states[symbol]
	if !ok {
		return
	}
	st.mu.Lock()
	st.recentTrades = make([]domain.FundingTradeRecord, 0, len(trades))
	for _, t := range trades {
		st.recentTrades = append(st.recentTrades, domain.FundingTradeRecord{
			ID:     t.ID,
			Amount: t.Amount,
			Rate:   t.Rate,
			Period: t.Period,
			MTS:    t.MTS,
		})
	}
	st.mu.Unlock()
}

func (s *Service) handleTradeExecuted(symbol string, trade bitfinex.FundingTrade) {
	st, ok := s.states[symbol]
	if !ok {
		return
	}
	st.mu.Lock()
	st.recentTrades = append(st.recentTrades, domain.FundingTradeRecord{
		ID:     trade.ID,
		Amount: trade.Amount,
		Rate:   trade.Rate,
		Period: trade.Period,
		MTS:    trade.MTS,
	})
	st.mu.Unlock()
}

// --- Snapshot loop ---

func (s *Service) snapshotLoop(ctx context.Context) {
	defer close(s.outCh) // snapshotLoop owns the channel lifetime
	ticker := time.NewTicker(s.cfg.SnapshotInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			for _, sym := range s.cfg.Symbols {
				snap := s.buildSnapshot(sym, now)
				if snap == nil {
					continue
				}
				// Send to output channel (non-blocking)
				select {
				case s.outCh <- snap:
				default:
					s.log.Warn("snapshot channel full, dropping", zap.String("symbol", sym))
				}
				// Cache to Redis
				if s.cache != nil {
					if err := s.cache.Set(ctx, sym, snap, snapshotCacheTTL); err != nil {
						s.log.Warn("cache snapshot failed", zap.Error(err))
					}
				}
				// Publish to PubSub so Hub (and other subscribers) receive snapshots
				if s.pubsub != nil {
					if err := s.pubsub.Publish(ctx, snap); err != nil {
						s.log.Warn("pubsub publish snapshot failed", zap.Error(err))
					}
				}
			}
		}
	}
}

func (s *Service) buildSnapshot(symbol string, now time.Time) *domain.MarketSnapshot {
	st, ok := s.states[symbol]
	if !ok {
		return nil
	}

	st.mu.Lock()
	// Skip if no ticker data yet
	if st.ticker == nil {
		st.mu.Unlock()
		return nil
	}

	// Copy data and prune trades under a single lock
	tickerCopy := *st.ticker
	bookEntries := make([]domain.BookEntry, 0, len(st.book))
	for _, e := range st.book {
		bookEntries = append(bookEntries, e)
	}
	cutoff := now.Add(-tradeBufferDuration)
	trades := make([]domain.FundingTradeRecord, 0, len(st.recentTrades))
	for _, t := range st.recentTrades {
		if t.MTS.After(cutoff) {
			trades = append(trades, t)
		}
	}
	st.recentTrades = trades
	st.mu.Unlock()

	// Dust filter
	filtered := orderbook.FilterDust(bookEntries, nil)

	raw := &domain.RawMarketData{
		Symbol:       symbol,
		Ticker:       &tickerCopy,
		Book:         filtered,
		RecentTrades: trades,
		Timestamp:    now,
	}

	// Phase 1.5: Flash crash detection
	flashFreeze := s.flashCrash.Check(&tickerCopy, now)

	// Phase 2: Signal computation
	var signals []domain.SignalValue
	for _, src := range s.signalSources {
		sig := src.Compute(raw)
		signals = append(signals, sig)
	}

	// Phase 2.5: Signal health tracking + MDC aggregation with recovery smoothing (§2.4)
	s.healthTracker.Update(signals, now)
	health := s.healthTracker.GetHealth(now, s.cfg.SnapshotInterval)

	// Build recovery weights for recovering signals (§2.4: 33%→66%→100%)
	recWeights := make(map[domain.SignalType]float64)
	for sigType, state := range health {
		if state == domain.SignalRecovering {
			recWeights[sigType] = s.healthTracker.RecoveryWeight(sigType)
		}
	}

	mdc := s.mdcAgg.Aggregate(signals, health, now, recWeights)

	// Phase 3: Regime detection
	regime, regimeParams := s.regimeDet[symbol].Detect(mdc, &tickerCopy, flashFreeze, now)

	// Order book analysis
	hiddenRatio := s.hiddenEst[symbol].Estimate(filtered, trades, now)
	competitorActivity := s.compDet[symbol].Analyze(filtered)

	// Check for degraded mode
	degradedMode := false
	degradedReason := ""
	if state, ok := health[domain.SignalBookConsumption]; ok && (state == domain.SignalDegraded || state == domain.SignalRecovering) {
		degradedMode = true
		degradedReason = "order_book_failure"
	}

	return assembleSnapshot(symbol, raw, signals, mdc, regime, regimeParams, flashFreeze, hiddenRatio, competitorActivity, health, degradedMode, degradedReason, now)
}

func bookEntryKey(rate float64, period int) string {
	// Use rate with enough precision to distinguish entries
	return formatFloat(rate) + ":" + formatInt(period)
}

func formatFloat(f float64) string {
	return strconv.FormatFloat(f, 'g', -1, 64)
}

func formatInt(i int) string {
	return strconv.Itoa(i)
}
