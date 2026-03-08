## ADDED Requirements

### Requirement: Acquire checks both global and per-user quota
Acquire SHALL return true only when both the global remaining quota ≥ n AND the user's remaining quota ≥ n. It SHALL atomically decrement both counters.

#### Scenario: Both sufficient
- **WHEN** global remaining=50, user remaining=10, Acquire(userID, 3)
- **THEN** returns true, global remaining=47, user remaining=7

#### Scenario: Global insufficient
- **WHEN** global remaining=2, user remaining=10, Acquire(userID, 3)
- **THEN** returns false, counters unchanged

#### Scenario: User insufficient
- **WHEN** global remaining=50, user remaining=2, Acquire(userID, 3)
- **THEN** returns false, counters unchanged

### Requirement: Release returns unused quota
Release SHALL increment both the global and per-user counters by n, not exceeding their maximum limits.

#### Scenario: Release unused
- **WHEN** user used 5 of 10 quota, Release(userID, 2)
- **THEN** user remaining becomes 7, global remaining incremented by 2

### Requirement: SetUserQuota configures per-user limit
SetUserQuota SHALL set the maximum per-window quota for the given userID. If user already exists, it SHALL update the limit.

#### Scenario: Set new user
- **WHEN** SetUserQuota("u1", 15)
- **THEN** user "u1" has quota limit=15, remaining=15

#### Scenario: Update existing user
- **WHEN** user "u1" has limit=10, SetUserQuota("u1", 20)
- **THEN** user "u1" limit updated to 20, remaining adjusted

### Requirement: RemoveUser cleans up
RemoveUser SHALL remove the user's quota entry and return their remaining quota to the global pool.

#### Scenario: Remove user
- **WHEN** user "u1" has remaining=8, RemoveUser("u1")
- **THEN** user "u1" entry removed, Remaining("u1") returns 0

### Requirement: Remaining query
Remaining SHALL return the user's remaining quota for the current window. Returns 0 if user does not exist.

#### Scenario: Query remaining
- **WHEN** user "u1" has limit=15, used=5
- **THEN** Remaining("u1") = 10

### Requirement: Refill resets all counters
Refill SHALL reset the global used counter to 0 and all per-user used counters to 0.

#### Scenario: Refill after usage
- **WHEN** global used=40, user "u1" used=8, user "u2" used=5, Refill()
- **THEN** global remaining=max, user "u1" remaining=limit, user "u2" remaining=limit

### Requirement: Thread safety
All Allocator operations SHALL be safe for concurrent use from multiple goroutines.

#### Scenario: Concurrent acquire
- **WHEN** 10 goroutines call Acquire simultaneously
- **THEN** no data races, total acquired ≤ global limit
