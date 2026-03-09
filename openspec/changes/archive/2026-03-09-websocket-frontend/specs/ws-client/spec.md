## ADDED Requirements

### Requirement: WebSocket client connection
The WSClient SHALL connect to the backend WebSocket endpoint using a short-lived ws-token obtained from the API proxy.

#### Scenario: Successful connection
- **WHEN** connect() is called
- **THEN** the client SHALL request a ws-token via `POST /api/proxy/auth/ws-token`, then open a WebSocket connection to `NEXT_PUBLIC_WS_URL/api/v1/ws?token=<token>`

#### Scenario: Token request failure
- **WHEN** the ws-token request fails (e.g., 401 unauthorized)
- **THEN** the client SHALL NOT attempt a WS connection and SHALL report the error

### Requirement: Auto-reconnect with exponential backoff
The WSClient SHALL automatically reconnect when the connection is lost, using exponential backoff starting at 1 second up to a maximum of 30 seconds.

#### Scenario: Connection lost
- **WHEN** the WebSocket connection closes unexpectedly
- **THEN** the client SHALL wait for the backoff interval and attempt to reconnect with a fresh ws-token

#### Scenario: Successful reconnect resets backoff
- **WHEN** a reconnection succeeds
- **THEN** the backoff interval SHALL reset to 1 second

#### Scenario: Intentional disconnect
- **WHEN** disconnect() is called explicitly
- **THEN** the client SHALL NOT attempt to reconnect

### Requirement: Message handling
The WSClient SHALL parse incoming messages and invoke a callback for each recognized message type.

#### Scenario: Snapshot message received
- **WHEN** a message with `{"type":"snapshot","data":{...}}` is received
- **THEN** the client SHALL invoke the onSnapshot callback with the parsed data

### Requirement: Heartbeat
The WSClient SHALL respond to server ping frames automatically (handled by browser WebSocket API).

#### Scenario: Server sends ping
- **WHEN** the server sends a WebSocket ping frame
- **THEN** the browser automatically responds with a pong frame, keeping the connection alive
