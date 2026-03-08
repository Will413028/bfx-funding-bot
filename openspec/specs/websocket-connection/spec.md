## ADDED Requirements

### Requirement: WebSocket connection lifecycle
WSClient SHALL manage the full connection lifecycle: connect, authenticate, maintain, and disconnect to `wss://api-pub.bitfinex.com/ws/2` (public) or `wss://api.bitfinex.com/ws/2` (authenticated).

#### Scenario: Successful public connection
- **WHEN** WSClient.Connect() is called without API credentials
- **THEN** the client SHALL establish a WebSocket connection to the public endpoint and receive an info event with `version: 2`

#### Scenario: Successful authenticated connection
- **WHEN** WSClient.Connect() is called with valid API key and secret
- **THEN** the client SHALL connect to the authenticated endpoint, send an auth event with HMAC-SHA384 signature, and receive `status: "OK"` on chanId 0

#### Scenario: Authentication with DMS and filter
- **WHEN** WSClient authenticates
- **THEN** the auth payload SHALL include `"dms": 4` and `"filter": ["funding-FOF", "funding-FCS", "wallet", "notify"]`

#### Scenario: Authentication failure
- **WHEN** WSClient authenticates with invalid credentials
- **THEN** the client SHALL receive `status: "FAILED"`, invoke the error handler, and NOT attempt to resubscribe

### Requirement: Heartbeat monitoring
WSClient SHALL monitor heartbeat messages to detect connection loss.

#### Scenario: Normal heartbeat
- **WHEN** the server sends `[CHANNEL_ID, "hb"]` within 20 seconds
- **THEN** the client SHALL reset the heartbeat timer for that connection

#### Scenario: Heartbeat timeout
- **WHEN** no message is received for 20 seconds
- **THEN** the client SHALL close the connection and trigger automatic reconnection

### Requirement: Automatic reconnection with exponential backoff
WSClient SHALL automatically reconnect when the connection is lost.

#### Scenario: Connection lost
- **WHEN** the WebSocket connection drops unexpectedly
- **THEN** the client SHALL attempt to reconnect with exponential backoff (1s, 2s, 4s, 8s, max 30s)

#### Scenario: Reconnect code 20051
- **WHEN** the client receives info event with code 20051
- **THEN** the client SHALL immediately close and reconnect

#### Scenario: Maintenance mode 20060/20061
- **WHEN** the client receives code 20060
- **THEN** the client SHALL pause all activity and wait
- **WHEN** the client receives code 20061
- **THEN** the client SHALL reconnect and resubscribe all channels

#### Scenario: Successful reconnection
- **WHEN** the client reconnects successfully
- **THEN** the client SHALL re-authenticate (if credentials provided) and resubscribe all previously subscribed channels

### Requirement: Graceful shutdown
WSClient SHALL support graceful shutdown.

#### Scenario: Close called
- **WHEN** WSClient.Close() is called
- **THEN** the client SHALL send a close frame, stop the read loop, cancel reconnection attempts, and release all resources

### Requirement: Channel ID management
WSClient SHALL maintain a mapping of channel IDs to subscription info.

#### Scenario: Subscription confirmed
- **WHEN** the server responds with a `subscribed` event containing `chanId`
- **THEN** the client SHALL store the mapping from chanId to the channel type and symbol

#### Scenario: Data message routing
- **WHEN** a data message `[chanId, ...]` is received
- **THEN** the client SHALL look up the channel info by chanId and route to the appropriate handler
