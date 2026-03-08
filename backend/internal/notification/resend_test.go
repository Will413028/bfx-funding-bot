package notification

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"

	"github.com/resend/resend-go/v2"
)

func testResendClient(t *testing.T, srvURL string) *resend.Client {
	t.Helper()
	client := resend.NewClient("re_test_key")
	u, err := url.Parse(srvURL + "/")
	if err != nil {
		t.Fatal(err)
	}
	client.BaseURL = u
	return client
}

func TestResendNotifier_SendWelcome(t *testing.T) {
	var captured resend.SendEmailRequest

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewDecoder(r.Body).Decode(&captured)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{"id": "test-id-123"})
	}))
	defer srv.Close()

	n := &ResendNotifier{client: testResendClient(t, srv.URL), from: "bot@example.com"}

	err := n.SendWelcome(context.Background(), "user@example.com")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if captured.Subject != "Welcome to BFX Funding Bot" {
		t.Errorf("expected welcome subject, got %q", captured.Subject)
	}
	if len(captured.To) != 1 || captured.To[0] != "user@example.com" {
		t.Errorf("expected to=user@example.com, got %v", captured.To)
	}
	if captured.From != "bot@example.com" {
		t.Errorf("expected from=bot@example.com, got %q", captured.From)
	}
}

func TestResendNotifier_SendAPIKeyAlert(t *testing.T) {
	var captured resend.SendEmailRequest

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewDecoder(r.Body).Decode(&captured)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{"id": "test-id-456"})
	}))
	defer srv.Close()

	n := &ResendNotifier{client: testResendClient(t, srv.URL), from: "bot@example.com"}

	err := n.SendAPIKeyAlert(context.Background(), "user@example.com", "Key expired")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if captured.Subject != "API Key Alert — BFX Funding Bot" {
		t.Errorf("expected alert subject, got %q", captured.Subject)
	}
	if captured.Text == "" {
		t.Error("expected non-empty text body")
	}
}

func TestResendNotifier_SendAlert(t *testing.T) {
	var captured resend.SendEmailRequest

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewDecoder(r.Body).Decode(&captured)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{"id": "test-id-789"})
	}))
	defer srv.Close()

	n := &ResendNotifier{client: testResendClient(t, srv.URL), from: "bot@example.com"}

	err := n.SendAlert(context.Background(), "user@example.com", "Custom Subject", "Custom message")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if captured.Subject != "Custom Subject" {
		t.Errorf("expected custom subject, got %q", captured.Subject)
	}
	if captured.Text != "Custom message" {
		t.Errorf("expected custom message, got %q", captured.Text)
	}
}

func TestResendNotifier_APIError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{"message": "invalid api key"})
	}))
	defer srv.Close()

	n := &ResendNotifier{client: testResendClient(t, srv.URL), from: "bot@example.com"}

	err := n.SendWelcome(context.Background(), "user@example.com")
	if err == nil {
		t.Fatal("expected error for bad API key")
	}
}
