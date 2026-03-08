package notification

import "context"

// Notifier defines the contract for sending notifications.
type Notifier interface {
	SendWelcome(ctx context.Context, email string) error
	SendAPIKeyAlert(ctx context.Context, email, message string) error
	SendAlert(ctx context.Context, email, subject, message string) error
}
