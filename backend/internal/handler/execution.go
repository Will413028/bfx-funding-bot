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

type ExecutionHandler struct {
	svc *service.ExecutionService
}

func NewExecutionHandler(svc *service.ExecutionService) *ExecutionHandler {
	return &ExecutionHandler{svc: svc}
}

// List handles cursor-based pagination for executions.
// First page: GET /executions (or GET /executions?limit=50)
// Next pages: GET /executions?after=<cursor>&limit=50
func (h *ExecutionHandler) List(c *gin.Context) {
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

	pagination := executionPagination(records, limit)
	if len(records) > limit {
		records = records[:limit]
	}

	c.JSON(http.StatusOK, gin.H{
		"data":       records,
		"pagination": pagination,
	})
}

func executionPagination(records []domain.ExecutionRecord, limit int) PaginationResponse {
	if len(records) <= limit {
		return PaginationResponse{HasMore: false}
	}
	last := records[limit-1]
	return PaginationResponse{
		NextCursor: EncodeCursor(last.CreatedAt, last.ID),
		HasMore:    true,
	}
}

func parseIntQuery(c *gin.Context, key string) int {
	if l := c.Query(key); l != "" {
		n, _ := strconv.Atoi(l)
		return n
	}
	return 0
}
