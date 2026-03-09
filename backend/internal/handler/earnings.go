package handler

import (
	"net/http"
	"strconv"

	"github.com/gin-gonic/gin"

	"github.com/will/bfx-funding-bot/backend/internal/middleware"
	"github.com/will/bfx-funding-bot/backend/internal/service"
)

type EarningsHandler struct {
	svc *service.EarningsService
}

func NewEarningsHandler(svc *service.EarningsService) *EarningsHandler {
	return &EarningsHandler{svc: svc}
}

func (h *EarningsHandler) Get(c *gin.Context) {
	userID := c.GetString(middleware.ContextUserID)
	summary, err := h.svc.GetEarnings(c.Request.Context(), userID)
	if err != nil {
		handleError(c, err)
		return
	}

	c.JSON(http.StatusOK, gin.H{"data": summary})
}

func (h *EarningsHandler) History(c *gin.Context) {
	userID := c.GetString(middleware.ContextUserID)

	days := 30
	if d := c.Query("days"); d != "" {
		if parsed, err := strconv.Atoi(d); err == nil && parsed > 0 {
			days = parsed
		}
	}

	history, err := h.svc.GetEarningsHistory(c.Request.Context(), userID, days)
	if err != nil {
		handleError(c, err)
		return
	}

	c.JSON(http.StatusOK, gin.H{"data": history})
}
