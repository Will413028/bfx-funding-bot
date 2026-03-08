package handler

import (
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/middleware"
	"github.com/will/bfx-funding-bot/backend/internal/service"
)

type BillingHandler struct {
	svc *service.BillingService
}

func NewBillingHandler(svc *service.BillingService) *BillingHandler {
	return &BillingHandler{svc: svc}
}

func (h *BillingHandler) Get(c *gin.Context) {
	userID := c.GetString(middleware.ContextUserID)

	// Cursor-based pagination mode
	if after := c.Query("after"); after != "" {
		h.listCursor(c, userID, after)
		return
	}

	// Legacy mode (since + limit)
	var since time.Time
	if s := c.Query("since"); s != "" {
		t, err := time.Parse(time.RFC3339, s)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{
				"error": gin.H{"code": "INVALID_PARAM", "message": "since must be RFC3339 format"},
			})
			return
		}
		since = t
	}

	limit := 12
	if l := c.Query("limit"); l != "" {
		n, err := strconv.Atoi(l)
		if err != nil || n < 1 {
			c.JSON(http.StatusBadRequest, gin.H{
				"error": gin.H{"code": "INVALID_PARAM", "message": "limit must be a positive integer"},
			})
			return
		}
		limit = n
	}

	summary, err := h.svc.GetBilling(c.Request.Context(), userID, since, limit)
	if err != nil {
		handleError(c, err)
		return
	}

	c.JSON(http.StatusOK, summary)
}

// ListPaginated handles cursor-based pagination for billing records.
func (h *BillingHandler) ListPaginated(c *gin.Context) {
	userID := c.GetString(middleware.ContextUserID)
	h.listCursor(c, userID, c.Query("after"))
}

func (h *BillingHandler) listCursor(c *gin.Context, userID, after string) {
	limit := ClampLimit(parseIntQuery(c, "limit"), 20, 100)

	var cursorTime *time.Time
	var cursorID string
	if after != "" {
		cursor, err := DecodeCursor(after)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{
				"error": gin.H{"code": "INVALID_PARAM", "message": "invalid cursor"},
			})
			return
		}
		cursorTime = &cursor.Timestamp
		cursorID = cursor.ID
	}

	records, err := h.svc.ListByUserPaginated(c.Request.Context(), userID, cursorTime, cursorID, limit+1)
	if err != nil {
		handleError(c, err)
		return
	}

	pagination := billingPagination(records, limit)
	if len(records) > limit {
		records = records[:limit]
	}

	c.JSON(http.StatusOK, gin.H{
		"records":    records,
		"pagination": pagination,
	})
}

func billingPagination(records []domain.BillingRecord, limit int) PaginationResponse {
	if len(records) <= limit {
		return PaginationResponse{HasMore: false}
	}
	last := records[limit-1]
	return PaginationResponse{
		NextCursor: EncodeCursor(last.CreatedAt, last.ID),
		HasMore:    true,
	}
}

func (h *BillingHandler) GetPlan(c *gin.Context) {
	userID := c.GetString(middleware.ContextUserID)

	features, err := h.svc.GetPlan(c.Request.Context(), userID)
	if err != nil {
		handleError(c, err)
		return
	}

	c.JSON(http.StatusOK, features)
}
