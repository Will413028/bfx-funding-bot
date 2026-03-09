package handler

import (
	"context"
	"encoding/json"
	"net/http"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
	"go.uber.org/zap"

	"github.com/will/bfx-funding-bot/backend/internal/auth"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/repository"
)

const (
	writeWait      = 10 * time.Second
	pongWait       = 60 * time.Second
	pingPeriod     = 30 * time.Second
	maxMessageSize = 512
	sendBufSize    = 16
)

var upgrader = websocket.Upgrader{
	ReadBufferSize:  1024,
	WriteBufferSize: 1024,
	CheckOrigin: func(r *http.Request) bool {
		return true // CORS handled at middleware level
	},
}

// wsMessage is the envelope for all WebSocket messages.
type wsMessage struct {
	Type string          `json:"type"`
	Data json.RawMessage `json:"data"`
}

// ---------- Client ----------

type client struct {
	hub    *Hub
	conn   *websocket.Conn
	send   chan []byte
	userID string
}

// readPump reads messages from the client (only pong frames matter).
func (c *client) readPump() {
	defer func() {
		c.hub.unregister <- c
		c.conn.Close()
	}()

	c.conn.SetReadLimit(maxMessageSize)
	c.conn.SetReadDeadline(time.Now().Add(pongWait))
	c.conn.SetPongHandler(func(string) error {
		c.conn.SetReadDeadline(time.Now().Add(pongWait))
		return nil
	})

	for {
		if _, _, err := c.conn.ReadMessage(); err != nil {
			break
		}
	}
}

// writePump writes messages to the client and sends periodic pings.
func (c *client) writePump() {
	ticker := time.NewTicker(pingPeriod)
	defer func() {
		ticker.Stop()
		c.conn.Close()
	}()

	for {
		select {
		case msg, ok := <-c.send:
			c.conn.SetWriteDeadline(time.Now().Add(writeWait))
			if !ok {
				c.conn.WriteMessage(websocket.CloseMessage, nil)
				return
			}
			if err := c.conn.WriteMessage(websocket.TextMessage, msg); err != nil {
				return
			}
		case <-ticker.C:
			c.conn.SetWriteDeadline(time.Now().Add(writeWait))
			if err := c.conn.WriteMessage(websocket.PingMessage, nil); err != nil {
				return
			}
		}
	}
}

// ---------- Hub ----------

// Hub manages all active WebSocket clients and broadcasts snapshots.
type Hub struct {
	clients    map[*client]bool
	register   chan *client
	unregister chan *client
	broadcast  chan []byte

	pubsub repository.SnapshotPubSub
	log    *zap.Logger
	wsMgr  *auth.WSTokenManager

	cancel context.CancelFunc
	wg     sync.WaitGroup
}

func NewHub(pubsub repository.SnapshotPubSub, wsMgr *auth.WSTokenManager, log *zap.Logger) *Hub {
	return &Hub{
		clients:    make(map[*client]bool),
		register:   make(chan *client),
		unregister: make(chan *client),
		broadcast:  make(chan []byte, 64),
		pubsub:     pubsub,
		wsMgr:      wsMgr,
		log:        log,
	}
}

// Run starts the Hub's main event loop and snapshot subscription.
func (h *Hub) Run(ctx context.Context) {
	ctx, h.cancel = context.WithCancel(ctx)

	h.wg.Add(1)
	go h.eventLoop(ctx)

	h.wg.Add(1)
	go h.subscribeSnapshots(ctx)
}

func (h *Hub) eventLoop(ctx context.Context) {
	defer h.wg.Done()

	for {
		select {
		case <-ctx.Done():
			// Close all clients on shutdown
			for c := range h.clients {
				close(c.send)
				delete(h.clients, c)
			}
			return

		case c := <-h.register:
			h.clients[c] = true
			h.log.Debug("ws client registered", zap.String("userID", c.userID), zap.Int("total", len(h.clients)))

		case c := <-h.unregister:
			if _, ok := h.clients[c]; ok {
				delete(h.clients, c)
				close(c.send)
				h.log.Debug("ws client unregistered", zap.String("userID", c.userID), zap.Int("total", len(h.clients)))
			}

		case msg := <-h.broadcast:
			for c := range h.clients {
				select {
				case c.send <- msg:
				default:
					// Slow client: close connection
					delete(h.clients, c)
					close(c.send)
					h.log.Warn("ws client too slow, disconnecting", zap.String("userID", c.userID))
				}
			}
		}
	}
}

// subscribeSnapshots subscribes to Redis snapshot pub/sub and forwards to broadcast.
func (h *Hub) subscribeSnapshots(ctx context.Context) {
	defer h.wg.Done()

	ch, err := h.pubsub.Subscribe(ctx)
	if err != nil {
		h.log.Error("failed to subscribe to snapshot channel", zap.Error(err))
		return
	}

	for {
		select {
		case <-ctx.Done():
			return
		case snap, ok := <-ch:
			if !ok {
				return
			}
			h.broadcastSnapshot(snap)
		}
	}
}

func (h *Hub) broadcastSnapshot(snap *domain.MarketSnapshot) {
	data, err := json.Marshal(snap)
	if err != nil {
		h.log.Error("failed to marshal snapshot for ws", zap.Error(err))
		return
	}

	msg, err := json.Marshal(wsMessage{
		Type: "snapshot",
		Data: data,
	})
	if err != nil {
		h.log.Error("failed to marshal ws message", zap.Error(err))
		return
	}

	select {
	case h.broadcast <- msg:
	default:
		h.log.Warn("ws broadcast channel full, dropping snapshot")
	}
}

// Close gracefully shuts down the Hub.
func (h *Hub) Close() {
	if h.cancel != nil {
		h.cancel()
	}
	h.wg.Wait()
}

// ---------- HTTP Handler ----------

// HandleWS upgrades an HTTP connection to WebSocket after validating the ws-token.
func (h *Hub) HandleWS(c *gin.Context) {
	token := c.Query("token")
	if token == "" {
		c.JSON(http.StatusUnauthorized, gin.H{
			"error": gin.H{"code": "MISSING_TOKEN", "message": "WebSocket token required"},
		})
		return
	}

	userID, err := h.wsMgr.Validate(c.Request.Context(), token)
	if err != nil {
		c.JSON(http.StatusUnauthorized, gin.H{
			"error": gin.H{"code": "INVALID_TOKEN", "message": "Invalid or expired WebSocket token"},
		})
		return
	}

	conn, err := upgrader.Upgrade(c.Writer, c.Request, nil)
	if err != nil {
		h.log.Error("ws upgrade failed", zap.Error(err), zap.String("userID", userID))
		return
	}

	cl := &client{
		hub:    h,
		conn:   conn,
		send:   make(chan []byte, sendBufSize),
		userID: userID,
	}

	h.register <- cl
	go cl.writePump()
	go cl.readPump()
}
