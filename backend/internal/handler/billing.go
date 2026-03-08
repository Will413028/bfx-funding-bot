package handler

import (
	"net/http"
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

// Get handles cursor-based pagination for billing records.
// First page: GET /billing (or GET /billing?limit=20)
// Next pages: GET /billing?after=<cursor>&limit=20
func (h *BillingHandler) Get(c *gin.Context) {
	userID := c.GetString(middleware.ContextUserID)
	limit := ClampLimit(parseIntQuery(c, "limit"), 20, 100)

	var cursorTime *time.Time
	var cursorID string
	if after := c.Query("after"); after != "" {
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
		"data":       records,
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

	c.JSON(http.StatusOK, gin.H{"data": features})
}
