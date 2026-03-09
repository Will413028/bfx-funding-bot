## ADDED Requirements

### Requirement: Zustand WebSocket store
The ws-store SHALL maintain the latest MarketSnapshot and connection status, accessible from any component.

#### Scenario: Snapshot received
- **WHEN** WSClient receives a snapshot message
- **THEN** the store SHALL update its `snapshot` state with the new data

#### Scenario: Connection status tracking
- **WHEN** the WS connection state changes (connecting/connected/disconnected)
- **THEN** the store SHALL update its `status` field accordingly

### Requirement: Dashboard WebSocket hook
The useDashboardWS hook SHALL manage the WS connection lifecycle tied to the component mount/unmount cycle.

#### Scenario: Component mount
- **WHEN** the Overview page mounts
- **THEN** useDashboardWS SHALL initiate the WS connection

#### Scenario: Component unmount
- **WHEN** the Overview page unmounts
- **THEN** useDashboardWS SHALL disconnect the WS connection

### Requirement: React Query cache sync
When a new MarketSnapshot arrives via WS, the dashboard React Query cache SHALL be updated so MarketPanel re-renders without a full REST refetch.

#### Scenario: WS snapshot updates cache
- **WHEN** a snapshot is received via WS
- **THEN** the hook SHALL call `queryClient.setQueryData` to update the market portion of the dashboard cache
