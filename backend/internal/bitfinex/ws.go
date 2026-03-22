package bitfinex

import (
	"encoding/json"
	"fmt"
	"math"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"
	"go.uber.org/zap"
)

const (
	wsPublicURL      = "wss://api-pub.bitfinex.com/ws/2"
	wsAuthURL        = "wss://api.bitfinex.com/ws/2"
	heartbeatTimeout = 20 * time.Second
	maxBackoff       = 30 * time.Second
	initialBackoff   = 1 * time.Second
)

// WSClient manages a WebSocket connection to Bitfinex.
type WSClient struct {
	handlers      EventHandlers
	channels      map[int]ChannelInfo
	connDone      chan struct{}
	done          chan struct{}
	log           *zap.Logger
	conn          *websocket.Conn
	apiSecret     string
	urlOverride   string
	apiKey        string
	subscriptions []Subscription
	mu            sync.RWMutex
	writeMu       sync.Mutex
	authenticated bool
	maintenance   bool
	closed        bool
}

// NewWSClient creates a new WebSocket client.
// If apiKey and apiSecret are empty, only public channels are available.
func NewWSClient(log *zap.Logger, apiKey, apiSecret string, handlers EventHandlers) *WSClient {
	return &WSClient{
		log:       log,
		apiKey:    apiKey,
		apiSecret: apiSecret,
		handlers:  handlers,
		channels:  make(map[int]ChannelInfo),
		done:      make(chan struct{}),
	}
}

// Connect establishes the WebSocket connection and starts the read loop.
func (ws *WSClient) Connect() error {
	// Guard against reconnect() calling Connect() after Close() has been called.
	ws.mu.RLock()
	if ws.closed {
		ws.mu.RUnlock()
		return fmt.Errorf("client is closed")
	}
	ws.mu.RUnlock()

	url := wsPublicURL
	if ws.apiKey != "" {
		url = wsAuthURL
	}
	if ws.urlOverride != "" {
		url = ws.urlOverride
	}

	conn, _, err := websocket.DefaultDialer.Dial(url, nil)
	if err != nil {
		return fmt.Errorf("websocket dial: %w", err)
	}

	ws.mu.Lock()
	// Close previous connDone to stop any lingering heartbeat goroutine.
	if ws.connDone != nil {
		close(ws.connDone)
	}
	ws.connDone = make(chan struct{})
	ws.conn = conn
	ws.channels = make(map[int]ChannelInfo)
	connDone := ws.connDone
	ws.mu.Unlock()

	go ws.readLoop(connDone)

	return nil
}

// Close gracefully shuts down the WebSocket connection.
func (ws *WSClient) Close() {
	ws.mu.Lock()
	defer ws.mu.Unlock()

	if ws.closed {
		return
	}
	ws.closed = true
	close(ws.done)

	// Stop the current connection's heartbeat goroutine.
	if ws.connDone != nil {
		close(ws.connDone)
		ws.connDone = nil
	}

	if ws.conn != nil {
		ws.writeMu.Lock()
		_ = ws.conn.WriteMessage(websocket.CloseMessage,
			websocket.FormatCloseMessage(websocket.CloseNormalClosure, ""))
		ws.writeMu.Unlock()
		_ = ws.conn.Close()
	}
}

// SubscribeTicker subscribes to a funding ticker channel.
func (ws *WSClient) SubscribeTicker(symbol string) error {
	ws.addSubscription(Subscription{Channel: "ticker", Symbol: symbol})
	return ws.sendJSON(map[string]string{
		"event":   "subscribe",
		"channel": "ticker",
		"symbol":  symbol,
	})
}

// SubscribeBook subscribes to a funding order book channel.
func (ws *WSClient) SubscribeBook(symbol, prec string, length int) error {
	ws.addSubscription(Subscription{Channel: "book", Symbol: symbol, Prec: prec, Len: length})
	return ws.sendJSON(map[string]any{
		"event":   "subscribe",
		"channel": "book",
		"symbol":  symbol,
		"prec":    prec,
		"len":     fmt.Sprintf("%d", length),
	})
}

// SubscribeTrades subscribes to a funding trades channel.
func (ws *WSClient) SubscribeTrades(symbol string) error {
	ws.addSubscription(Subscription{Channel: "trades", Symbol: symbol})
	return ws.sendJSON(map[string]string{
		"event":   "subscribe",
		"channel": "trades",
		"symbol":  symbol,
	})
}

// Unsubscribe unsubscribes from a channel by its chanId.
func (ws *WSClient) Unsubscribe(chanId int) error {
	return ws.sendJSON(map[string]any{
		"event":  "unsubscribe",
		"chanId": chanId,
	})
}

func (ws *WSClient) addSubscription(sub Subscription) {
	ws.mu.Lock()
	defer ws.mu.Unlock()
	// avoid duplicates
	for _, s := range ws.subscriptions {
		if s.Channel == sub.Channel && s.Symbol == sub.Symbol {
			return
		}
	}
	ws.subscriptions = append(ws.subscriptions, sub)
}

func (ws *WSClient) authenticate() error {
	nonce := generateNonce()
	authPayload, sig := computeWSSignature(nonce, ws.apiSecret)

	return ws.sendJSON(map[string]any{
		"event":       "auth",
		"apiKey":      ws.apiKey,
		"authSig":     sig,
		"authNonce":   nonce,
		"authPayload": authPayload,
		"dms":         4,
		"filter":      []string{"funding-FOF", "funding-FCS", "wallet", "notify"},
	})
}

func (ws *WSClient) resubscribe() {
	ws.mu.RLock()
	subs := make([]Subscription, len(ws.subscriptions))
	copy(subs, ws.subscriptions)
	ws.mu.RUnlock()

	for _, sub := range subs {
		var err error
		switch sub.Channel {
		case "ticker":
			err = ws.sendJSON(map[string]string{
				"event": "subscribe", "channel": "ticker", "symbol": sub.Symbol,
			})
		case "book":
			err = ws.sendJSON(map[string]any{
				"event": "subscribe", "channel": "book", "symbol": sub.Symbol,
				"prec": sub.Prec, "len": fmt.Sprintf("%d", sub.Len),
			})
		case "trades":
			err = ws.sendJSON(map[string]string{
				"event": "subscribe", "channel": "trades", "symbol": sub.Symbol,
			})
		}
		if err != nil {
			ws.log.Error("resubscribe failed", zap.String("channel", sub.Channel), zap.Error(err))
		}
	}
}

func (ws *WSClient) sendJSON(v any) error {
	ws.mu.RLock()
	conn := ws.conn
	ws.mu.RUnlock()

	if conn == nil {
		return fmt.Errorf("not connected")
	}

	ws.writeMu.Lock()
	defer ws.writeMu.Unlock()
	return conn.WriteJSON(v)
}

// readLoop reads messages from the WebSocket and dispatches them.
// connDone is closed when this connection is replaced (reconnect) or the client is shut down,
// which ensures the heartbeat goroutine exits promptly.
func (ws *WSClient) readLoop(connDone <-chan struct{}) {
	// Store last heartbeat time atomically to avoid timer concurrency issues.
	// The goroutine periodically checks this value instead of using timer.Reset
	// from the main loop (which would be a data race on time.Timer).
	var lastMsg atomic.Int64
	lastMsg.Store(time.Now().UnixNano())

	go func() {
		ticker := time.NewTicker(5 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				last := time.Unix(0, lastMsg.Load())
				if time.Since(last) > heartbeatTimeout {
					ws.log.Warn("heartbeat timeout, reconnecting")
					ws.mu.RLock()
					conn := ws.conn
					ws.mu.RUnlock()
					if conn != nil {
						_ = conn.Close()
					}
					return
				}
			case <-connDone:
				return
			}
		}
	}()

	for {
		ws.mu.RLock()
		conn := ws.conn
		ws.mu.RUnlock()
		if conn == nil {
			return
		}

		_, msg, err := conn.ReadMessage()
		if err != nil {
			select {
			case <-ws.done:
				return // graceful shutdown
			default:
			}
			ws.log.Warn("read error, will reconnect", zap.Error(err))
			if ws.handlers.OnDisconnect != nil {
				ws.handlers.OnDisconnect(err)
			}
			ws.reconnect()
			return
		}

		// Record last message time atomically (read by heartbeat goroutine)
		lastMsg.Store(time.Now().UnixNano())

		ws.handleMessage(msg)
	}
}

func (ws *WSClient) handleMessage(msg []byte) {
	// Try event object first (info, subscribed, auth, error)
	var event map[string]json.RawMessage
	if err := json.Unmarshal(msg, &event); err == nil {
		if evtRaw, ok := event["event"]; ok {
			var evtType string
			_ = json.Unmarshal(evtRaw, &evtType)
			ws.handleEvent(evtType, event)
			return
		}
	}

	// Otherwise it's a channel data array: [chanId, ...]
	var raw []json.RawMessage
	if err := json.Unmarshal(msg, &raw); err != nil || len(raw) < 2 {
		return
	}

	var chanId int
	if err := json.Unmarshal(raw[0], &chanId); err != nil {
		return
	}

	// Check for heartbeat
	var hb string
	if json.Unmarshal(raw[1], &hb) == nil && hb == "hb" {
		return
	}

	if chanId == 0 {
		ws.handleAuthData(raw)
	} else {
		ws.handleChannelData(chanId, raw)
	}
}

func (ws *WSClient) handleEvent(evtType string, event map[string]json.RawMessage) {
	switch evtType {
	case "info":
		ws.handleInfoEvent(event)
	case "subscribed":
		ws.handleSubscribedEvent(event)
	case "unsubscribed":
		ws.handleUnsubscribedEvent(event)
	case "auth":
		ws.handleAuthEvent(event)
	case "error":
		var msg string
		var code int
		if raw, ok := event["msg"]; ok {
			_ = json.Unmarshal(raw, &msg)
		}
		if raw, ok := event["code"]; ok {
			_ = json.Unmarshal(raw, &code)
		}
		ws.log.Error("ws error event", zap.Int("code", code), zap.String("msg", msg))
		if ws.handlers.OnError != nil {
			ws.handlers.OnError(fmt.Errorf("ws error %d: %s", code, msg))
		}
	}
}

func (ws *WSClient) handleInfoEvent(event map[string]json.RawMessage) {
	// Check for maintenance / reconnect codes
	if codeRaw, ok := event["code"]; ok {
		var code int
		_ = json.Unmarshal(codeRaw, &code)

		switch code {
		case 20051: // Reconnection requested
			ws.log.Info("received reconnect request (20051)")
			ws.mu.RLock()
			conn := ws.conn
			ws.mu.RUnlock()
			if conn != nil {
				_ = conn.Close()
			}
			// readLoop will detect close and call reconnect
		case 20060: // Entering maintenance
			ws.log.Info("entering maintenance mode (20060)")
			ws.mu.Lock()
			ws.maintenance = true
			ws.mu.Unlock()
		case 20061: // Maintenance ended
			ws.log.Info("maintenance ended (20061), reconnecting")
			ws.mu.Lock()
			ws.maintenance = false
			ws.mu.Unlock()
			ws.mu.RLock()
			conn := ws.conn
			ws.mu.RUnlock()
			if conn != nil {
				_ = conn.Close()
			}
		}
		return
	}

	// Initial info event with version
	ws.log.Info("connected to Bitfinex WebSocket")
	if ws.handlers.OnConnect != nil {
		ws.handlers.OnConnect()
	}

	// Authenticate if credentials provided
	if ws.apiKey != "" {
		if err := ws.authenticate(); err != nil {
			ws.log.Error("authentication failed", zap.Error(err))
			if ws.handlers.OnError != nil {
				ws.handlers.OnError(err)
			}
		}
	}
}

func (ws *WSClient) handleSubscribedEvent(event map[string]json.RawMessage) {
	var chanId int
	var channel, symbol string

	_ = json.Unmarshal(event["chanId"], &chanId)
	_ = json.Unmarshal(event["channel"], &channel)
	_ = json.Unmarshal(event["symbol"], &symbol)

	info := ChannelInfo{
		Channel: channel,
		Symbol:  symbol,
	}
	if precRaw, ok := event["prec"]; ok {
		_ = json.Unmarshal(precRaw, &info.Prec)
	}
	if lenRaw, ok := event["len"]; ok {
		var l string
		_ = json.Unmarshal(lenRaw, &l)
		_, _ = fmt.Sscanf(l, "%d", &info.Len)
	}

	ws.mu.Lock()
	ws.channels[chanId] = info
	ws.mu.Unlock()

	ws.log.Debug("subscribed", zap.Int("chanId", chanId),
		zap.String("channel", channel), zap.String("symbol", symbol))
}

func (ws *WSClient) handleUnsubscribedEvent(event map[string]json.RawMessage) {
	var chanId int
	_ = json.Unmarshal(event["chanId"], &chanId)

	ws.mu.Lock()
	info, ok := ws.channels[chanId]
	delete(ws.channels, chanId)
	// Remove from subscriptions
	if ok {
		for i, s := range ws.subscriptions {
			if s.Channel == info.Channel && s.Symbol == info.Symbol {
				ws.subscriptions = append(ws.subscriptions[:i], ws.subscriptions[i+1:]...)
				break
			}
		}
	}
	ws.mu.Unlock()

	ws.log.Debug("unsubscribed", zap.Int("chanId", chanId))
}

func (ws *WSClient) handleAuthEvent(event map[string]json.RawMessage) {
	var status string
	_ = json.Unmarshal(event["status"], &status)

	if status == "OK" {
		ws.mu.Lock()
		ws.authenticated = true
		ws.mu.Unlock()
		ws.log.Info("authenticated successfully")
		ws.resubscribe()
	} else {
		var msg string
		var code int
		if raw, ok := event["msg"]; ok {
			_ = json.Unmarshal(raw, &msg)
		}
		if raw, ok := event["code"]; ok {
			_ = json.Unmarshal(raw, &code)
		}
		ws.log.Error("authentication failed", zap.String("status", status),
			zap.Int("code", code), zap.String("msg", msg))
		if ws.handlers.OnError != nil {
			ws.handlers.OnError(fmt.Errorf("auth failed: %s (code %d)", msg, code))
		}
	}
}

// handleChannelData routes data to the appropriate public channel handler.
func (ws *WSClient) handleChannelData(chanId int, raw []json.RawMessage) {
	ws.mu.RLock()
	info, ok := ws.channels[chanId]
	ws.mu.RUnlock()
	if !ok {
		return
	}

	switch info.Channel {
	case "ticker":
		ws.handleTickerData(info.Symbol, raw[1])
	case "book":
		ws.handleBookData(info.Symbol, raw[1])
	case "trades":
		ws.handleTradesData(info.Symbol, raw)
	}
}

// --- Ticker parsing ---

func (ws *WSClient) handleTickerData(symbol string, data json.RawMessage) {
	if ws.handlers.OnTicker == nil {
		return
	}

	var arr []json.RawMessage
	if err := json.Unmarshal(data, &arr); err != nil || len(arr) < 16 {
		return
	}

	ticker := parseFundingTicker(symbol, arr)
	ws.handlers.OnTicker(ticker)
}

func parseFundingTicker(symbol string, arr []json.RawMessage) FundingTicker {
	t := FundingTicker{Symbol: symbol}
	_ = json.Unmarshal(arr[0], &t.FRR)
	_ = json.Unmarshal(arr[1], &t.Bid)
	parseIntField(arr[2], &t.BidPeriod)
	_ = json.Unmarshal(arr[3], &t.BidSize)
	_ = json.Unmarshal(arr[4], &t.Ask)
	parseIntField(arr[5], &t.AskPeriod)
	_ = json.Unmarshal(arr[6], &t.AskSize)
	_ = json.Unmarshal(arr[7], &t.DailyChange)
	_ = json.Unmarshal(arr[8], &t.DailyChangePerc)
	_ = json.Unmarshal(arr[9], &t.LastPrice)
	_ = json.Unmarshal(arr[10], &t.Volume)
	_ = json.Unmarshal(arr[11], &t.High)
	_ = json.Unmarshal(arr[12], &t.Low)
	_ = json.Unmarshal(arr[15], &t.FRRAmountAvail)
	return t
}

// --- Book parsing ---

func (ws *WSClient) handleBookData(symbol string, data json.RawMessage) {
	// Determine if snapshot (array of arrays) or update (single array)
	var snapshot [][]json.RawMessage
	if err := json.Unmarshal(data, &snapshot); err == nil && len(snapshot) > 0 {
		// Check if first element is an array (snapshot) or a number (single update)
		var testNum float64
		if json.Unmarshal(snapshot[0][0], &testNum) == nil && len(snapshot[0]) == 4 {
			// Could be snapshot or single update wrapped differently
			// If inner arrays exist, it's a snapshot
			if len(snapshot) > 1 || isNestedArray(data) {
				entries := make([]BookEntry, 0, len(snapshot))
				for _, item := range snapshot {
					if len(item) >= 4 {
						entries = append(entries, parseBookEntry(symbol, item))
					}
				}
				if ws.handlers.OnBookSnapshot != nil {
					ws.handlers.OnBookSnapshot(symbol, entries)
				}
				return
			}
		}
	}

	// Single update: [RATE, PERIOD, COUNT, AMOUNT]
	var single []json.RawMessage
	if err := json.Unmarshal(data, &single); err == nil && len(single) >= 4 {
		entry := parseBookEntry(symbol, single)
		if ws.handlers.OnBookUpdate != nil {
			ws.handlers.OnBookUpdate(symbol, entry)
		}
	}
}

func isNestedArray(data json.RawMessage) bool {
	// Check if data is [[...], [...]] vs [num, num, num, num]
	trimmed := data
	for len(trimmed) > 0 && (trimmed[0] == ' ' || trimmed[0] == '\t') {
		trimmed = trimmed[1:]
	}
	// [[  means nested
	return len(trimmed) > 1 && trimmed[0] == '[' && trimmed[1] == '['
}

func parseBookEntry(symbol string, arr []json.RawMessage) BookEntry {
	e := BookEntry{Symbol: symbol}
	_ = json.Unmarshal(arr[0], &e.Rate)
	parseIntField(arr[1], &e.Period)
	parseIntField(arr[2], &e.Count)
	_ = json.Unmarshal(arr[3], &e.Amount)
	return e
}

// --- Trades parsing ---

func (ws *WSClient) handleTradesData(symbol string, raw []json.RawMessage) {
	if len(raw) < 2 {
		return
	}

	// Check for "fte" or "ftu" type string at index 1
	var msgType string
	if json.Unmarshal(raw[1], &msgType) == nil && (msgType == "fte" || msgType == "ftu") {
		// Single trade: [chanId, "fte"/"ftu", [ID, MTS, AMOUNT, RATE, PERIOD]]
		if len(raw) < 3 {
			return
		}
		var tradeArr []json.RawMessage
		if err := json.Unmarshal(raw[2], &tradeArr); err != nil || len(tradeArr) < 5 {
			return
		}
		trade := parseFundingTrade(symbol, tradeArr)
		if msgType == "fte" && ws.handlers.OnTradeExecuted != nil {
			ws.handlers.OnTradeExecuted(symbol, trade)
		} else if msgType == "ftu" && ws.handlers.OnTradeUpdated != nil {
			ws.handlers.OnTradeUpdated(symbol, trade)
		}
		return
	}

	// Snapshot: [chanId, [[ID, MTS, AMOUNT, RATE, PERIOD], ...]]
	var snapshot [][]json.RawMessage
	if err := json.Unmarshal(raw[1], &snapshot); err != nil {
		return
	}
	trades := make([]FundingTrade, 0, len(snapshot))
	for _, item := range snapshot {
		if len(item) >= 5 {
			trades = append(trades, parseFundingTrade(symbol, item))
		}
	}
	if ws.handlers.OnTradeSnapshot != nil {
		ws.handlers.OnTradeSnapshot(symbol, trades)
	}
}

func parseFundingTrade(symbol string, arr []json.RawMessage) FundingTrade {
	t := FundingTrade{Symbol: symbol}
	_ = json.Unmarshal(arr[0], &t.ID)
	var mts int64
	_ = json.Unmarshal(arr[1], &mts)
	t.MTS = time.UnixMilli(mts)
	_ = json.Unmarshal(arr[2], &t.Amount)
	_ = json.Unmarshal(arr[3], &t.Rate)
	parseIntField(arr[4], &t.Period)
	return t
}

// --- Auth channel data ---

func (ws *WSClient) handleAuthData(raw []json.RawMessage) {
	if len(raw) < 3 {
		return
	}

	var msgType string
	if err := json.Unmarshal(raw[1], &msgType); err != nil {
		return
	}

	switch msgType {
	// Funding Offers
	case "fos":
		ws.handleFundingOfferSnapshot(raw[2])
	case "fon":
		ws.handleFundingOfferSingle(raw[2], ws.handlers.OnFundingOfferNew)
	case "fou":
		ws.handleFundingOfferSingle(raw[2], ws.handlers.OnFundingOfferUpdate)
	case "foc":
		ws.handleFundingOfferSingle(raw[2], ws.handlers.OnFundingOfferCancel)

	// Funding Credits
	case "fcs":
		ws.handleFundingCreditSnapshot(raw[2])
	case "fcn":
		ws.handleFundingCreditSingle(raw[2], ws.handlers.OnFundingCreditNew)
	case "fcu":
		ws.handleFundingCreditSingle(raw[2], ws.handlers.OnFundingCreditUpdate)
	case "fcc":
		ws.handleFundingCreditSingle(raw[2], ws.handlers.OnFundingCreditClose)

	// Wallets
	case "ws":
		ws.handleWalletSnapshot(raw[2])
	case "wu":
		ws.handleWalletUpdate(raw[2])

	// Notifications
	case "n":
		ws.handleNotification(raw[2])
	}
}

func (ws *WSClient) handleFundingOfferSnapshot(data json.RawMessage) {
	if ws.handlers.OnFundingOfferSnapshot == nil {
		return
	}
	var items [][]json.RawMessage
	if err := json.Unmarshal(data, &items); err != nil {
		return
	}
	offers := make([]WSFundingOffer, 0, len(items))
	for _, item := range items {
		offers = append(offers, parseWSFundingOffer(item))
	}
	ws.handlers.OnFundingOfferSnapshot(offers)
}

func (ws *WSClient) handleFundingOfferSingle(data json.RawMessage, handler func(WSFundingOffer)) {
	if handler == nil {
		return
	}
	var arr []json.RawMessage
	if err := json.Unmarshal(data, &arr); err != nil {
		return
	}
	handler(parseWSFundingOffer(arr))
}

func parseWSFundingOffer(arr []json.RawMessage) WSFundingOffer {
	o := WSFundingOffer{}
	if len(arr) < 21 {
		return o
	}
	_ = json.Unmarshal(arr[0], &o.ID)
	_ = json.Unmarshal(arr[1], &o.Symbol)
	var createdMs, updatedMs int64
	_ = json.Unmarshal(arr[2], &createdMs)
	_ = json.Unmarshal(arr[3], &updatedMs)
	o.Created = time.UnixMilli(createdMs)
	o.Updated = time.UnixMilli(updatedMs)
	_ = json.Unmarshal(arr[4], &o.Amount)
	_ = json.Unmarshal(arr[5], &o.AmountOrig)
	_ = json.Unmarshal(arr[6], &o.Type)
	_ = json.Unmarshal(arr[10], &o.Status)
	_ = json.Unmarshal(arr[14], &o.Rate)
	parseIntField(arr[15], &o.Period)
	var notify, hidden, renew int
	_ = json.Unmarshal(arr[16], &notify)
	_ = json.Unmarshal(arr[17], &hidden)
	_ = json.Unmarshal(arr[19], &renew)
	o.Notify = notify == 1
	o.Hidden = hidden == 1
	o.Renew = renew == 1
	return o
}

func (ws *WSClient) handleFundingCreditSnapshot(data json.RawMessage) {
	if ws.handlers.OnFundingCreditSnapshot == nil {
		return
	}
	var items [][]json.RawMessage
	if err := json.Unmarshal(data, &items); err != nil {
		return
	}
	credits := make([]WSFundingCredit, 0, len(items))
	for _, item := range items {
		credits = append(credits, parseWSFundingCredit(item))
	}
	ws.handlers.OnFundingCreditSnapshot(credits)
}

func (ws *WSClient) handleFundingCreditSingle(data json.RawMessage, handler func(WSFundingCredit)) {
	if handler == nil {
		return
	}
	var arr []json.RawMessage
	if err := json.Unmarshal(data, &arr); err != nil {
		return
	}
	handler(parseWSFundingCredit(arr))
}

func parseWSFundingCredit(arr []json.RawMessage) WSFundingCredit {
	c := WSFundingCredit{}
	if len(arr) < 22 {
		return c
	}
	_ = json.Unmarshal(arr[0], &c.ID)
	_ = json.Unmarshal(arr[1], &c.Symbol)
	_ = json.Unmarshal(arr[2], &c.Side)
	var createdMs, updatedMs int64
	_ = json.Unmarshal(arr[3], &createdMs)
	_ = json.Unmarshal(arr[4], &updatedMs)
	c.Created = time.UnixMilli(createdMs)
	c.Updated = time.UnixMilli(updatedMs)
	_ = json.Unmarshal(arr[5], &c.Amount)
	_ = json.Unmarshal(arr[7], &c.Status)
	_ = json.Unmarshal(arr[11], &c.Rate)
	parseIntField(arr[12], &c.Period)
	var openMs int64
	_ = json.Unmarshal(arr[13], &openMs)
	c.Opened = time.UnixMilli(openMs)
	var renew, noClose int
	_ = json.Unmarshal(arr[18], &renew)
	_ = json.Unmarshal(arr[20], &noClose)
	c.Renew = renew == 1
	c.NoClose = noClose == 1
	if len(arr) > 21 {
		_ = json.Unmarshal(arr[21], &c.PositionPair)
	}
	return c
}

func (ws *WSClient) handleWalletSnapshot(data json.RawMessage) {
	if ws.handlers.OnWalletSnapshot == nil {
		return
	}
	var items [][]json.RawMessage
	if err := json.Unmarshal(data, &items); err != nil {
		return
	}
	wallets := make([]WSWallet, 0)
	for _, item := range items {
		w := parseWSWallet(item)
		if w.Type == "funding" {
			wallets = append(wallets, w)
		}
	}
	ws.handlers.OnWalletSnapshot(wallets)
}

func (ws *WSClient) handleWalletUpdate(data json.RawMessage) {
	if ws.handlers.OnWalletUpdate == nil {
		return
	}
	var arr []json.RawMessage
	if err := json.Unmarshal(data, &arr); err != nil {
		return
	}
	w := parseWSWallet(arr)
	if w.Type == "funding" {
		ws.handlers.OnWalletUpdate(w)
	}
}

func parseWSWallet(arr []json.RawMessage) WSWallet {
	w := WSWallet{}
	if len(arr) < 5 {
		return w
	}
	_ = json.Unmarshal(arr[0], &w.Type)
	_ = json.Unmarshal(arr[1], &w.Currency)
	_ = json.Unmarshal(arr[2], &w.Balance)
	_ = json.Unmarshal(arr[3], &w.Unsettled)
	_ = json.Unmarshal(arr[4], &w.Available)
	return w
}

func (ws *WSClient) handleNotification(data json.RawMessage) {
	if ws.handlers.OnNotification == nil {
		return
	}
	var arr []json.RawMessage
	if err := json.Unmarshal(data, &arr); err != nil || len(arr) < 8 {
		return
	}

	n := WSNotification{}
	var mts int64
	_ = json.Unmarshal(arr[0], &mts)
	n.MTS = time.UnixMilli(mts)
	_ = json.Unmarshal(arr[1], &n.Type)
	_ = json.Unmarshal(arr[2], &n.MessageID)
	n.NotifyInfo = []byte(arr[4])
	_ = json.Unmarshal(arr[5], &n.Code)
	_ = json.Unmarshal(arr[6], &n.Status)
	_ = json.Unmarshal(arr[7], &n.Text)

	ws.handlers.OnNotification(n)
}

// --- Reconnection ---

func (ws *WSClient) reconnect() {
	backoff := initialBackoff

	for {
		select {
		case <-ws.done:
			return
		default:
		}

		// Check maintenance mode
		ws.mu.RLock()
		inMaintenance := ws.maintenance
		ws.mu.RUnlock()
		if inMaintenance {
			time.Sleep(1 * time.Second)
			continue
		}

		ws.log.Info("attempting reconnection", zap.Duration("backoff", backoff))
		time.Sleep(backoff)

		select {
		case <-ws.done:
			return
		default:
		}

		if err := ws.Connect(); err != nil {
			ws.log.Warn("reconnection failed", zap.Error(err))
			backoff = time.Duration(math.Min(float64(backoff*2), float64(maxBackoff)))
			continue
		}

		ws.log.Info("reconnected successfully")
		// resubscribe happens after auth (or immediately for public-only)
		if ws.apiKey == "" {
			ws.resubscribe()
		}
		// For authenticated connections, resubscribe happens after auth success
		// (the info event handler calls authenticate, and after auth success we resubscribe)
		return
	}
}

// parseIntField handles JSON numbers that may be float or int.
func parseIntField(raw json.RawMessage, target *int) {
	var f float64
	if json.Unmarshal(raw, &f) == nil {
		*target = int(f)
	}
}
