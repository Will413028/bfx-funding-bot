## ADDED Requirements

### Requirement: Funding ticker subscription
WSClient SHALL support subscribing to funding ticker channels.

#### Scenario: Subscribe to funding ticker
- **WHEN** WSClient.SubscribeTicker("fUSD") is called
- **THEN** the client SHALL send `{"event":"subscribe","channel":"ticker","symbol":"fUSD"}` and store the subscription for reconnection

#### Scenario: Ticker snapshot received
- **WHEN** the server sends a ticker snapshot `[chanId, [FRR, BID, BID_PERIOD, BID_SIZE, ASK, ASK_PERIOD, ASK_SIZE, DAILY_CHANGE, DAILY_CHANGE_PERC, LAST_PRICE, VOLUME, HIGH, LOW, _, _, FRR_AMOUNT_AVAIL]]`
- **THEN** the client SHALL parse all 16 fields into a FundingTicker struct and invoke the OnTicker handler

#### Scenario: Ticker update received
- **WHEN** the server sends a ticker update (same format as snapshot but single array)
- **THEN** the client SHALL parse and invoke the OnTicker handler

### Requirement: Funding order book subscription
WSClient SHALL support subscribing to funding order book channels.

#### Scenario: Subscribe to funding book
- **WHEN** WSClient.SubscribeBook("fUSD", "P0", 25) is called
- **THEN** the client SHALL send `{"event":"subscribe","channel":"book","symbol":"fUSD","prec":"P0","len":"25"}`

#### Scenario: Book snapshot received
- **WHEN** the server sends a book snapshot `[chanId, [[RATE, PERIOD, COUNT, AMOUNT], ...]]`
- **THEN** the client SHALL parse each entry into a BookEntry struct and invoke the OnBookSnapshot handler

#### Scenario: Book update received
- **WHEN** the server sends a book update `[chanId, [RATE, PERIOD, COUNT, AMOUNT]]`
- **THEN** the client SHALL parse the entry and invoke the OnBookUpdate handler

#### Scenario: Book entry deletion
- **WHEN** a book update has `COUNT = 0`
- **THEN** the client SHALL invoke the OnBookUpdate handler with the entry marked for deletion (`AMOUNT = 1` for offer side, `AMOUNT = -1` for bid side)

### Requirement: Funding trades subscription
WSClient SHALL support subscribing to funding trades channels.

#### Scenario: Subscribe to funding trades
- **WHEN** WSClient.SubscribeTrades("fUSD") is called
- **THEN** the client SHALL send `{"event":"subscribe","channel":"trades","symbol":"fUSD"}`

#### Scenario: Trades snapshot received
- **WHEN** the server sends a trades snapshot `[chanId, [[ID, MTS, AMOUNT, RATE, PERIOD], ...]]`
- **THEN** the client SHALL parse each entry into a FundingTrade struct and invoke the OnTradeSnapshot handler

#### Scenario: Trade executed
- **WHEN** the server sends `[chanId, "fte", [ID, MTS, AMOUNT, RATE, PERIOD]]`
- **THEN** the client SHALL parse the trade and invoke the OnTradeExecuted handler

#### Scenario: Trade updated
- **WHEN** the server sends `[chanId, "ftu", [ID, MTS, AMOUNT, RATE, PERIOD]]`
- **THEN** the client SHALL parse the trade and invoke the OnTradeUpdated handler

### Requirement: Unsubscribe from channel
WSClient SHALL support unsubscribing from channels.

#### Scenario: Unsubscribe
- **WHEN** WSClient.Unsubscribe(chanId) is called
- **THEN** the client SHALL send `{"event":"unsubscribe","chanId":<chanId>}` and remove the channel from the subscription map upon confirmation

### Requirement: Heartbeat filtering
WSClient SHALL filter heartbeat messages from public channels.

#### Scenario: Public channel heartbeat
- **WHEN** the server sends `[chanId, "hb"]` on a public channel
- **THEN** the client SHALL NOT invoke any data handler, only reset the heartbeat timer
