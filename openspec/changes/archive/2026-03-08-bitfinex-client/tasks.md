## 1. Domain Models

- [x] 1.1 Create `domain/wallet.go` — Wallet struct (Currency, Balance, BalanceAvailable)
- [x] 1.2 Create `domain/offer.go` — FundingOffer struct (ID, Currency, Amount, Rate, Period, Status, timestamps) + OfferParams struct
- [x] 1.3 Create `domain/credit.go` — FundingCredit struct (ID, Currency, Amount, Rate, Period, Status, AutoRenew, OpenedAt, ExpiresAt)
- [x] 1.4 Add Bitfinex-related error factories to `domain/errors.go` — ErrBitfinexAPI, ErrInsufficientFunds, ErrInvalidAPIKey

## 2. Authentication

- [x] 2.1 Create `bitfinex/auth.go` — HMAC-SHA384 signature computation (path + nonce + body), nonce generation with atomic counter
- [x] 2.2 Write `bitfinex/auth_test.go` — test signature computation against known test vectors

## 3. Client Implementation

- [x] 3.1 Create `bitfinex/client.go` — Client struct with injectable *http.Client, NewClient constructor, base URL constant, authenticated request helper
- [x] 3.2 Implement error response parsing — parse Bitfinex error format `["error", code, message]` into domain.AppError
- [x] 3.3 Implement `VerifyCredentials(ctx, apiKey, apiSecret)` — call GET /v2/auth/r/wallets to verify key validity
- [x] 3.4 Implement `GetFundingBalance(ctx, apiKey, apiSecret, currency)` — parse wallets response, filter funding wallet, return domain.Wallet
- [x] 3.5 Implement `SubmitFundingOffer(ctx, apiKey, apiSecret, params)` — POST /v2/auth/w/funding/offer/submit, return domain.FundingOffer
- [x] 3.6 Implement `CancelFundingOffer(ctx, apiKey, apiSecret, offerID)` — POST /v2/auth/w/funding/offer/cancel
- [x] 3.7 Implement `GetActiveFundingOffers(ctx, apiKey, apiSecret, currency)` — POST /v2/auth/r/funding/offers/{currency}
- [x] 3.8 Implement `GetActiveFundingCredits(ctx, apiKey, apiSecret, currency)` — POST /v2/auth/r/funding/credits/{currency}

## 4. Tests

- [x] 4.1 Write `bitfinex/client_test.go` — use httptest.Server to mock Bitfinex API responses: verify credentials, get balance, submit/cancel offer, list offers/credits, error handling

## 5. DI Wiring

- [x] 5.1 Wire `bitfinex.NewClient` in `cmd/server/main.go` via fx.Provide (inject default http.Client)

## 6. Verification

- [x] 6.1 Run `go build ./...` to verify compilation
- [x] 6.2 Run `go test ./...` to verify all tests pass
