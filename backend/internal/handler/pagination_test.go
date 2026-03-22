package handler

import (
	"testing"
	"time"
)

func TestEncodeDecode_Roundtrip(t *testing.T) {
	ts := time.Date(2025, 1, 15, 10, 30, 0, 0, time.UTC)
	id := "abc-123-def"

	encoded := EncodeCursor(ts, id)
	decoded, err := DecodeCursor(encoded)
	if err != nil {
		t.Fatalf("decode error: %v", err)
	}

	if !decoded.Timestamp.Equal(ts) {
		t.Errorf("timestamp mismatch: got %v, want %v", decoded.Timestamp, ts)
	}
	if decoded.ID != id {
		t.Errorf("ID mismatch: got %s, want %s", decoded.ID, id)
	}
}

func TestDecodeCursor_Invalid(t *testing.T) {
	tests := []struct {
		name   string
		cursor string
	}{
		{"not base64", "!!!invalid!!!"},
		{"no colon", "bm9jb2xvbg=="}, // "nocolon" base64
		{"empty", ""},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := DecodeCursor(tt.cursor)
			if err == nil {
				t.Error("expected error for invalid cursor")
			}
		})
	}
}

func TestClampLimit(t *testing.T) {
	tests := []struct {
		limit, def, max, want int
	}{
		{0, 20, 100, 20},
		{-1, 20, 100, 20},
		{50, 20, 100, 50},
		{150, 20, 100, 100},
		{1, 20, 100, 1},
		{100, 20, 100, 100},
	}

	for _, tt := range tests {
		got := ClampLimit(tt.limit, tt.def, tt.max)
		if got != tt.want {
			t.Errorf("ClampLimit(%d, %d, %d) = %d, want %d", tt.limit, tt.def, tt.max, got, tt.want)
		}
	}
}
