## ADDED Requirements

### Requirement: Funding offers events
WSClient SHALL parse and deliver funding offer events from the authenticated channel (chanId 0).

#### Scenario: Offers snapshot
- **WHEN** the server sends `[0, "fos", [[offer_array], ...]]`
- **THEN** the client SHALL parse each offer array (ID, SYMBOL, AMOUNT, AMOUNT_ORIG, STATUS, RATE, PERIOD, RENEW) and invoke the OnFundingOfferSnapshot handler

#### Scenario: New offer
- **WHEN** the server sends `[0, "fon", [offer_array]]`
- **THEN** the client SHALL parse the offer and invoke the OnFundingOfferNew handler

#### Scenario: Offer update
- **WHEN** the server sends `[0, "fou", [offer_array]]`
- **THEN** the client SHALL parse the offer and invoke the OnFundingOfferUpdate handler

#### Scenario: Offer cancel
- **WHEN** the server sends `[0, "foc", [offer_array]]`
- **THEN** the client SHALL parse the offer and invoke the OnFundingOfferCancel handler

### Requirement: Funding credits events
WSClient SHALL parse and deliver funding credit events from the authenticated channel.

#### Scenario: Credits snapshot
- **WHEN** the server sends `[0, "fcs", [[credit_array], ...]]`
- **THEN** the client SHALL parse each credit (ID, SYMBOL, AMOUNT, STATUS, RATE, PERIOD, MTS_OPENING, RENEW) and invoke the OnFundingCreditSnapshot handler

#### Scenario: New credit
- **WHEN** the server sends `[0, "fcn", [credit_array]]`
- **THEN** the client SHALL parse and invoke the OnFundingCreditNew handler

#### Scenario: Credit update
- **WHEN** the server sends `[0, "fcu", [credit_array]]`
- **THEN** the client SHALL parse and invoke the OnFundingCreditUpdate handler

#### Scenario: Credit close
- **WHEN** the server sends `[0, "fcc", [credit_array]]`
- **THEN** the client SHALL parse and invoke the OnFundingCreditClose handler

### Requirement: Wallet updates
WSClient SHALL parse and deliver funding wallet updates from the authenticated channel.

#### Scenario: Wallet snapshot
- **WHEN** the server sends `[0, "ws", [[TYPE, CURRENCY, BALANCE, UNSETTLED, AVAILABLE, ...], ...]]`
- **THEN** the client SHALL filter entries where TYPE == "funding" and invoke the OnWalletSnapshot handler

#### Scenario: Wallet update
- **WHEN** the server sends `[0, "wu", [TYPE, CURRENCY, BALANCE, UNSETTLED, AVAILABLE, ...]]`
- **THEN** the client SHALL check if TYPE == "funding" and if so invoke the OnWalletUpdate handler

### Requirement: Notification events
WSClient SHALL parse notification events for operation result feedback.

#### Scenario: Success notification
- **WHEN** the server sends `[0, "n", [MTS, TYPE, MSG_ID, null, NOTIFY_INFO, CODE, "SUCCESS", TEXT]]`
- **THEN** the client SHALL invoke the OnNotification handler with status "SUCCESS"

#### Scenario: Error notification
- **WHEN** the server sends `[0, "n", [..., "ERROR", TEXT]]` or `[..., "FAILURE", TEXT]`
- **THEN** the client SHALL invoke the OnNotification handler with the error status and text

### Requirement: Authenticated channel heartbeat
WSClient SHALL handle heartbeats on the authenticated channel.

#### Scenario: Auth channel heartbeat
- **WHEN** the server sends `[0, "hb"]`
- **THEN** the client SHALL reset the heartbeat timer without invoking any data handler
