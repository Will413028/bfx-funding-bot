## ADDED Requirements

### Requirement: WebSocket upgrade endpoint
The system SHALL provide a `GET /api/v1/ws?token=<ws-token>` endpoint that upgrades the HTTP connection to WebSocket after validating the short-lived ws-token.

#### Scenario: Successful upgrade
- **WHEN** a client connects to `GET /api/v1/ws?token=<valid-token>`
- **THEN** the server SHALL upgrade the connection to WebSocket and register the client in the Hub

#### Scenario: Missing token
- **WHEN** a client connects to `GET /api/v1/ws` without a token parameter
- **THEN** the server SHALL respond with HTTP 401 and close the connection

#### Scenario: Invalid or expired token
- **WHEN** a client connects with an expired or non-existent token
- **THEN** the server SHALL respond with HTTP 401 and close the connection

### Requirement: Hub manages client connections
The Hub SHALL maintain a registry of active WebSocket clients. It SHALL support concurrent register, unregister, and broadcast operations via dedicated channels.

#### Scenario: Client registration
- **WHEN** a WebSocket connection is established
- **THEN** the Hub SHALL add the client to its registry

#### Scenario: Client disconnection
- **WHEN** a WebSocket client disconnects (close frame, read error, or pong timeout)
- **THEN** the Hub SHALL remove the client from its registry and close the send channel

### Requirement: Snapshot broadcast
The Hub SHALL subscribe to the Redis `market:snapshot:updates` Pub/Sub channel and broadcast each MarketSnapshot as a JSON message to all registered clients.

#### Scenario: New snapshot received
- **WHEN** a new MarketSnapshot is published on the Redis channel
- **THEN** the Hub SHALL serialize it as `{"type":"snapshot","data":{...}}` and send to all connected clients

#### Scenario: Slow client
- **WHEN** a client's send buffer is full (cannot keep up with broadcast rate)
- **THEN** the Hub SHALL close the client connection and unregister it

### Requirement: Heartbeat mechanism
The server SHALL send WebSocket ping frames every 30 seconds. If a client fails to respond with a pong within 60 seconds, the server SHALL close the connection.

#### Scenario: Healthy connection
- **WHEN** the server sends a ping frame
- **THEN** the client responds with a pong and the connection remains active

#### Scenario: Pong timeout
- **WHEN** 60 seconds pass without receiving a pong from a client
- **THEN** the server SHALL close the connection

### Requirement: Graceful shutdown
The Hub SHALL stop accepting new connections and close all existing connections when the application shuts down.

#### Scenario: Application shutdown
- **WHEN** the fx lifecycle triggers a stop signal
- **THEN** the Hub SHALL close all client connections and stop the broadcast goroutine
