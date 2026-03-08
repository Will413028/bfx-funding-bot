## 1. Schema & Domain

- [x] 1.1 Add `exchange_status` column to `api_keys` table in `schema/schema.hcl` (TEXT NOT NULL DEFAULT 'unverified')
- [x] 1.2 Generate Atlas migration (`atlas migrate diff --env neon`)
- [x] 1.3 Add `ExchangeStatus` field to `domain.APIKey` struct
- [x] 1.4 Update sqlc queries to include `exchange_status` in INSERT/SELECT/UPDATE for api_keys
- [x] 1.5 Run `sqlc generate` and update repository code for new column

## 2. Service Layer

- [x] 2.1 Add `*bitfinex.Client` dependency to `APIKeyService`, update constructor
- [x] 2.2 Modify `APIKeyService.Create` — after encryption, call `VerifyCredentials` and `GetFundingBalance`, set exchange_status accordingly
- [x] 2.3 Add `APIKeyService.Verify(ctx, userID, keyID)` — decrypt secret, call Bitfinex API, update exchange_status in DB, return verification result

## 3. Handler & Router

- [x] 3.1 Update `APIKeyHandler` Create response to include `exchange_status` and optional `funding_balance`
- [x] 3.2 Update List and GetByID responses to include `exchange_status`
- [x] 3.3 Add `APIKeyHandler.Verify` handler (POST `/api-keys/:id/verify`)
- [x] 3.4 Register verify route in router

## 4. DI Wiring

- [x] 4.1 Update `cmd/server/main.go` — pass `bitfinex.Client` to `NewAPIKeyService`

## 5. Tests

- [x] 5.1 Update `service/apikey_test.go` — test create with verification (success + failure), test verify method
- [x] 5.2 Update `handler/apikey_test.go` — test create response includes exchange_status, test verify endpoint

## 6. Verification

- [x] 6.1 Run `go build ./...` to verify compilation
- [x] 6.2 Run `go test ./...` to verify all tests pass
- [x] 6.3 Apply migration to Neon (`atlas migrate apply --env neon`)
