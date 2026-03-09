package handler

import (
	"net/http"

	"github.com/gin-gonic/gin"

	"github.com/will/bfx-funding-bot/backend/internal/auth"
	"github.com/will/bfx-funding-bot/backend/internal/middleware"
)

type WSTokenHandler struct {
	mgr *auth.WSTokenManager
}

func NewWSTokenHandler(mgr *auth.WSTokenManager) *WSTokenHandler {
	return &WSTokenHandler{mgr: mgr}
}

func (h *WSTokenHandler) Create(c *gin.Context) {
	userID := c.GetString(middleware.ContextUserID)

	token, err := h.mgr.Generate(c.Request.Context(), userID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{
			"error": gin.H{"code": "WS_TOKEN_ERROR", "message": "Failed to generate WebSocket token"},
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{"token": token})
}
