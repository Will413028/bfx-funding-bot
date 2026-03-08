package marketfeed

import "github.com/will/bfx-funding-bot/backend/internal/domain"

// SignalSource is the consumer-side interface for signal computation modules.
// Each signal module (C3) implements this interface and is injected into the Market Feed Service.
type SignalSource interface {
	Name() string
	Compute(data *domain.RawMarketData) domain.SignalValue
}
