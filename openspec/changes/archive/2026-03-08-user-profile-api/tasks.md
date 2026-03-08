## 1. Repository Layer

- [x] 1.1 Add `UpdatePassword(ctx, id, passwordHash)` SQL query to `query/user.sql`
- [x] 1.2 Run `sqlc generate` to regenerate Go code
- [x] 1.3 Add `UpdatePassword` to `UserRepository` interface in `repository/interfaces.go`
- [x] 1.4 Implement `UpdatePassword` in `repository/postgres/user.go`

## 2. Service Layer

- [x] 2.1 Add `GetProfile(ctx, userID)` method to `service/user.go` — calls repo.GetByID, returns user without password_hash
- [x] 2.2 Add `ChangePassword(ctx, userID, currentPassword, newPassword)` method to `service/user.go` — verify old password, validate new password length, hash and update

## 3. Handler & Router

- [x] 3.1 Create `handler/user.go` with `UserHandler` struct, `GetProfile` and `ChangePassword` methods
- [x] 3.2 Update `handler/router.go` to register `GET /me` and `PUT /me/password` under protected group, inject UserHandler in NewRouter

## 4. Tests

- [x] 4.1 Add service tests in `service/user_test.go` for GetProfile and ChangePassword (success, wrong password, short password)
- [x] 4.2 Create `handler/user_test.go` with handler tests for both endpoints
