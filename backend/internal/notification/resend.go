package notification

import (
	"context"
	"fmt"

	"github.com/resend/resend-go/v2"
)

// ResendNotifier implements Notifier using the Resend email API.
type ResendNotifier struct {
	client *resend.Client
	from   string
}

func NewResendNotifier(apiKey, fromEmail string) *ResendNotifier {
	return &ResendNotifier{
		client: resend.NewClient(apiKey),
		from:   fromEmail,
	}
}

func (n *ResendNotifier) SendWelcome(ctx context.Context, email string) error {
	_, err := n.client.Emails.SendWithContext(ctx, &resend.SendEmailRequest{
		From:    n.from,
		To:      []string{email},
		Subject: "Welcome to BFX Funding Bot",
		Text:    "Your account has been created successfully.\n\nYou can now configure your API key and lending strategy to start earning funding interest automatically.",
	})
	if err != nil {
		return fmt.Errorf("send welcome email: %w", err)
	}
	return nil
}

func (n *ResendNotifier) SendVerification(ctx context.Context, email, token string) error {
	_, err := n.client.Emails.SendWithContext(ctx, &resend.SendEmailRequest{
		From:    n.from,
		To:      []string{email},
		Subject: "Verify your email — BFX Funding Bot",
		Text:    fmt.Sprintf("Please verify your email address by using the following token:\n\n%s\n\nThis token expires in 24 hours. If you did not create an account, please ignore this email.", token),
	})
	if err != nil {
		return fmt.Errorf("send verification email: %w", err)
	}
	return nil
}

func (n *ResendNotifier) SendPasswordReset(ctx context.Context, email, token string) error {
	_, err := n.client.Emails.SendWithContext(ctx, &resend.SendEmailRequest{
		From:    n.from,
		To:      []string{email},
		Subject: "Reset your password — BFX Funding Bot",
		Text:    fmt.Sprintf("You requested a password reset. Use the following token:\n\n%s\n\nThis token expires in 1 hour. If you did not request this, please ignore this email.", token),
	})
	if err != nil {
		return fmt.Errorf("send password reset email: %w", err)
	}
	return nil
}

func (n *ResendNotifier) SendAPIKeyAlert(ctx context.Context, email, message string) error {
	_, err := n.client.Emails.SendWithContext(ctx, &resend.SendEmailRequest{
		From:    n.from,
		To:      []string{email},
		Subject: "API Key Alert — BFX Funding Bot",
		Text:    fmt.Sprintf("An issue was detected with your API key:\n\n%s\n\nPlease check your API key settings.", message),
	})
	if err != nil {
		return fmt.Errorf("send api key alert: %w", err)
	}
	return nil
}

func (n *ResendNotifier) SendAlert(ctx context.Context, email, subject, message string) error {
	_, err := n.client.Emails.SendWithContext(ctx, &resend.SendEmailRequest{
		From:    n.from,
		To:      []string{email},
		Subject: subject,
		Text:    message,
	})
	if err != nil {
		return fmt.Errorf("send alert: %w", err)
	}
	return nil
}
