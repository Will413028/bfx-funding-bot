package handler

import (
	"encoding/base64"
	"fmt"
	"strings"
	"time"
)

// Cursor encodes a position for keyset pagination.
type Cursor struct {
	Timestamp time.Time
	ID        string
}

// EncodeCursor encodes a timestamp + ID into a base64 cursor string.
func EncodeCursor(t time.Time, id string) string {
	raw := fmt.Sprintf("%d:%s", t.UnixMilli(), id)
	return base64.URLEncoding.EncodeToString([]byte(raw))
}

// DecodeCursor decodes a base64 cursor string into timestamp + ID.
func DecodeCursor(cursor string) (*Cursor, error) {
	data, err := base64.URLEncoding.DecodeString(cursor)
	if err != nil {
		return nil, fmt.Errorf("invalid cursor encoding")
	}

	parts := strings.SplitN(string(data), ":", 2)
	if len(parts) != 2 {
		return nil, fmt.Errorf("invalid cursor format")
	}

	var ms int64
	if _, err := fmt.Sscanf(parts[0], "%d", &ms); err != nil {
		return nil, fmt.Errorf("invalid cursor timestamp")
	}

	return &Cursor{
		Timestamp: time.UnixMilli(ms),
		ID:        parts[1],
	}, nil
}

// PaginationResponse is the pagination metadata included in list responses.
type PaginationResponse struct {
	NextCursor string `json:"nextCursor,omitempty"`
	HasMore    bool   `json:"hasMore"`
}

// ClampLimit clamps a limit value to the valid range [1, max].
func ClampLimit(limit, defaultLimit, maxLimit int) int {
	if limit <= 0 {
		return defaultLimit
	}
	if limit > maxLimit {
		return maxLimit
	}
	return limit
}
