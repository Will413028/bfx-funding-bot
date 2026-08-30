# Operator-only Containment and Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改動 account schema 的前提下，關閉所有自助註冊、把私人 API 收斂到唯一 operator、撤銷既有非 operator session，並把 process liveness 與 DB readiness 分開。

**Architecture:** Better Auth 的 sign-up endpoint 在 server-side hook 拒絕；前端移除 register surface；FastAPI 以 `BFX_OPERATOR_USER_ID` + `admin` role 建立 fail-closed `require_operator` dependency，所有現有 private `/api/v1` router 使用它；`/health` 只回報 process liveness，新增 `/ready` 檢查 DB session factory 與 `SELECT 1`。

**Tech Stack:** Next.js 16、Better Auth、TypeScript、FastAPI、Python 3.13、SQLAlchemy 2.0 async、PyJWT、pytest、Vitest/Playwright、mypy、ruff。

**Spec:** `docs/superpowers/specs/2026-08-31-production-integrity-foundation-design.md`（Release 0）。

## Global Constraints

- 只做 operator containment；不可在本計畫引入 `exchange_accounts` membership 或 account-scoped URL，這些由 Plan 2 的 Halt 1 處理。
- `BFX_OPERATOR_USER_ID` 缺失、空字串或 JWT role 不是 `admin` 時一律拒絕；不得 fallback 到第一個 user、`BFX_ACCOUNT_ID` 或 user profile。
- signup 必須在 Better Auth server endpoint 阻擋；只隱藏頁面不算完成。既有 `/api/auth/sign-up/email` direct request 仍須收到固定的 `signup_disabled` 錯誤。
- `/health` 不可因 DB 啟動失敗變成 5xx；`/ready` 在 DB 未設定、連線失敗或 migration 不完整時回 503，且不得洩漏 connection string。
- 所有後端測試從 `backend_py/` 使用 `uv run`；每個 production code change 先增加 failing test，完成後跑 targeted tests，再跑完整 non-integration suite。
- 不執行 remote command、container restart、deploy、session revoke production call 或 migration upgrade；本 plan 只建立程式與文件。

---

## File Structure

### New files

| File | Responsibility |
|---|---|
| `backend_py/tests/test_operator_auth.py` | operator identity、role、missing-config、invalid-token contract tests |
| `backend_py/tests/test_readiness.py` | liveness/readiness HTTP contract tests |
| `frontend/src/lib/__tests__/signup-disabled.test.ts` | server-side auth hook direct signup denial test |
| `frontend/e2e/operator-containment.spec.ts` | UI register absence、direct endpoint denial、login redirect smoke |

### Modified files

| File | Responsibility after this plan |
|---|---|
| `backend_py/src/bfx_funding_bot/core/settings.py` | `operator_user_id` 與 `operator_role` 設定 |
| `backend_py/src/bfx_funding_bot/core/auth.py` | `require_operator` dependency 與 fail-closed checks |
| `backend_py/src/bfx_funding_bot/modules/api/routers.py` | profile endpoint 改用 operator dependency |
| `backend_py/src/bfx_funding_bot/modules/api/api_keys.py` | private API key routes 改用 operator dependency |
| `backend_py/src/bfx_funding_bot/modules/api/config.py` | private config routes 改用 operator dependency |
| `backend_py/src/bfx_funding_bot/modules/api/attribution.py` | private attribution route 改用 operator dependency |
| `backend_py/src/bfx_funding_bot/modules/api/projections.py` | private projection routes 改用 operator dependency |
| `backend_py/src/bfx_funding_bot/modules/api/plans.py` | plan dependency 先從 operator dependency 開始 |
| `backend_py/src/bfx_funding_bot/modules/api/ratelimit.py` | rate limiter 不再自行建立 user-only auth dependency |
| `backend_py/src/bfx_funding_bot/main.py` | `/ready` 與 deterministic app state wiring |
| `frontend/src/lib/auth.ts` | Better Auth sign-up before hook、admin bootstrap contract、MFA requirement wiring |
| `frontend/src/app/api/auth/[...all]/route.ts` | 保留 Better Auth handler，確保 hook 覆蓋 direct endpoint |
| `frontend/src/app/api/proxy/[...path]/route.ts` | 只為完成 MFA 的 operator session mint backend JWT |
| `frontend/src/app/[locale]/(auth)/actions.ts` | 移除 `register` server action |
| `frontend/src/app/[locale]/(auth)/register/page.tsx` | 移除 public register page |
| `frontend/src/features/auth/components/register-form.tsx` | 移除 register component 與 import |
| `frontend/middleware.ts` | 移除 `/register` auth path，未登入仍只導向 `/login` |
| `.env.example` | document `BFX_OPERATOR_USER_ID` / `BFX_OPERATOR_ROLE` |
| `backend_py/.env.example` | document backend operator setting |
| `backend_py/ARCHITECTURE.md` | document Release 0 operator-only and readiness semantics |

## Task 1: Freeze the operator authorization contract

**Files:**
- Create: `backend_py/tests/test_operator_auth.py`
- Modify: `backend_py/src/bfx_funding_bot/core/settings.py`
- Modify: `backend_py/src/bfx_funding_bot/core/auth.py`

**Interfaces:**
- `Settings.operator_user_id: str` reads `BFX_OPERATOR_USER_ID` and defaults to the empty string only so test imports remain possible; `require_operator` treats empty as `auth_not_configured` (503).
- `Settings.operator_role: str = "admin"` reads `BFX_OPERATOR_ROLE`; production validation rejects any value other than `admin`.
- `async def require_operator(creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> Principal` first calls `_verify`, then returns only when `principal.user_id == settings.operator_user_id` and `principal.role == settings.operator_role`; all failures use stable details `auth_not_configured`, `operator_required`, or `operator_role_required`.

- [ ] **Step 1: Write failing unit tests**

```python
def test_require_operator_accepts_only_configured_admin(monkeypatch, token_factory):
    monkeypatch.setenv("BFX_OPERATOR_USER_ID", "operator-1")
    monkeypatch.setenv("BFX_OPERATOR_ROLE", "admin")
    monkeypatch.setattr(auth, "_verify", lambda _: Principal("operator-1", None, "admin"))
    assert asyncio.run(auth.require_operator(_credentials(token_factory()))) .user_id == "operator-1"


@pytest.mark.parametrize("principal", [
    Principal("other", None, "admin"),
    Principal("operator-1", None, "user"),
])
def test_require_operator_rejects_non_operator_or_non_admin(monkeypatch, principal):
    monkeypatch.setenv("BFX_OPERATOR_USER_ID", "operator-1")
    monkeypatch.setattr(auth, "_verify", lambda _: principal)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_operator(_credentials("token")))
    assert exc.value.status_code == 403


def test_missing_operator_id_fails_closed(monkeypatch):
    monkeypatch.delenv("BFX_OPERATOR_USER_ID", raising=False)
    monkeypatch.setattr(auth, "_verify", lambda _: Principal("operator-1", None, "admin"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_operator(_credentials("token")))
    assert exc.value.status_code == 503
    assert exc.value.detail == "auth_not_configured"
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run: `cd backend_py && uv run pytest tests/test_operator_auth.py -q`

Expected: FAIL because `Settings` has no operator fields and `require_operator` does not exist.

- [ ] **Step 3: Implement the dependency without changing JWT verification**

Read operator settings inside the dependency (or through an injected settings accessor) so test monkeypatches are honored; do not duplicate `_verify`, issuer, audience, or JWKS logic. Preserve `Principal` as the only JWT-derived value. Add a unit-level settings validator that raises a clear `ValueError` for `BFX_OPERATOR_ROLE != "admin"` when `BFX_PHASE=canary` or `BFX_PHASE=live`.

- [ ] **Step 4: Run focused tests and static checks**

Run: `cd backend_py && uv run pytest tests/test_operator_auth.py tests/test_jwks_auth.py -q && uv run mypy src/bfx_funding_bot/core/auth.py src/bfx_funding_bot/core/settings.py && uv run ruff check src/bfx_funding_bot/core/auth.py src/bfx_funding_bot/core/settings.py`

Expected: PASS.

- [ ] **Step 5: Commit the auth boundary**

```bash
git add backend_py/src/bfx_funding_bot/core/settings.py backend_py/src/bfx_funding_bot/core/auth.py backend_py/tests/test_operator_auth.py
git commit -m "feat: enforce operator-only backend authorization"
```

## Task 2: Put every private API router behind `require_operator`

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/api/routers.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/api/api_keys.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/api/config.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/api/attribution.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/api/projections.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/api/plans.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/api/ratelimit.py`
- Modify: `backend_py/tests/test_api_keys_router.py`
- Modify: `backend_py/tests/test_config_router.py`
- Modify: `backend_py/tests/test_projections_router.py`

**Interfaces:**
- `public.py` remains the only unauthenticated router and is not changed in this task.
- Every private route dependency annotation uses `Principal = Depends(require_operator)`; `require_user` remains available only for lower-level tests and future account membership composition.
- `shared_rate_limit_dependency()` receives the already-authorized principal through `require_operator`, so an unauthenticated or non-operator request cannot consume a bucket or discover route behavior.

- [ ] **Step 1: Add failing cross-router authorization tests**

For each private router, override `require_operator` in `app.dependency_overrides` with a valid and a non-operator fake dependency, then assert HTTP 403 for the latter; keep the existing valid-principal tests and assert public `/api/v1/public/*` behavior is unchanged. Add a test that a missing operator setting returns 503 before the handler queries the database.

- [ ] **Step 2: Run focused router tests and verify failure**

Run: `cd backend_py && uv run pytest tests/test_api_keys_router.py tests/test_config_router.py tests/test_projections_router.py -q`

Expected: FAIL because routes still depend on `require_user`.

- [ ] **Step 3: Replace dependencies and preserve non-enumerating errors**

Change only the auth dependency imports/annotations and the rate-limit dependency; do not alter existing response schemas or account filtering in this release. Ensure FastAPI dependency order evaluates operator auth before `get_session` for private routes.

- [ ] **Step 4: Run focused and full non-integration tests**

Run: `cd backend_py && uv run pytest tests/test_api_keys_router.py tests/test_config_router.py tests/test_projections_router.py tests/test_jwks_auth.py -q && uv run pytest -m "not integration" -q`

Expected: PASS.

- [ ] **Step 5: Commit private-router containment**

```bash
git add backend_py/src/bfx_funding_bot/modules/api/routers.py backend_py/src/bfx_funding_bot/modules/api/api_keys.py backend_py/src/bfx_funding_bot/modules/api/config.py backend_py/src/bfx_funding_bot/modules/api/attribution.py backend_py/src/bfx_funding_bot/modules/api/projections.py backend_py/src/bfx_funding_bot/modules/api/plans.py backend_py/src/bfx_funding_bot/modules/api/ratelimit.py backend_py/tests/test_api_keys_router.py backend_py/tests/test_config_router.py backend_py/tests/test_projections_router.py
git commit -m "refactor: gate private api routes by operator"
```

## Task 3: Disable Better Auth signup at the server boundary

**Files:**
- Create: `frontend/src/lib/__tests__/signup-disabled.test.ts`
- Modify: `frontend/src/lib/auth.ts`
- Modify: `frontend/src/app/api/proxy/[...path]/route.ts`
- Modify: `frontend/src/app/[locale]/(auth)/actions.ts`
- Delete: `frontend/src/app/[locale]/(auth)/register/page.tsx`
- Delete: `frontend/src/features/auth/components/register-form.tsx`
- Modify: `frontend/middleware.ts`

**Interfaces:**
- Better Auth `hooks.before` contains a path matcher for `/sign-up/email`; its handler throws a typed `APIError` with HTTP 403 and body detail `signup_disabled`. The handler executes before user creation, regardless of whether the caller is browser, server action, or direct HTTP.
- `actions.ts` exports `login` and `logout`; it no longer exports `register`.
- No locale route, link, middleware redirect, or client import references `/register` or `register-form`.
- Existing admin bootstrap remains explicit: the operator row is provisioned outside signup, and two-factor enrollment is required before the operator can obtain an execution-capable session/JWT.
- The BFF proxy reads the Better Auth session's two-factor verification state before calling getToken; an unverified operator receives 403 mfa_required and no backend request is made. The backend continues to require the operator/admin JWT contract.

- [ ] **Step 1: Write failing auth-hook and source-surface tests**

Mock the Better Auth context and call the exported hook handler with `{ path: "/sign-up/email" }`; assert the typed error and that the database adapter is not called. Add a source test that reads the route tree and fails if `/register` or `register-form` remains.

- [ ] **Step 2: Run frontend tests and verify failure**

Run: `cd frontend && pnpm test -- signup-disabled.test.ts`

Expected: FAIL because no signup hook exists and register files still exist.

- [ ] **Step 3: Implement the hook and remove the UI surface**

Use the installed Better Auth `createAuthMiddleware`/`APIError` types rather than an untyped cast. Keep rate limiting for `/sign-up/email` as defense in depth, but do not treat rate limiting as the signup control. Remove the action/page/component and the `/register` middleware entry; keep `/login` behavior unchanged.

Update the proxy to inspect the session returned by auth.api.getSession({ headers: await headers() }) before minting a JWT. Require twoFactorVerified === true for the configured operator; return a JSON 403 without calling the backend otherwise. Keep the existing path-prefix traversal check and browser-never-holds-JWT property.

- [ ] **Step 4: Verify direct endpoint and build**

Run: `cd frontend && pnpm test -- signup-disabled.test.ts && pnpm lint && pnpm build`

Expected: PASS; build has no dangling register imports.

- [ ] **Step 5: Commit frontend containment**

```bash
git add frontend/src/lib/auth.ts frontend/src/lib/__tests__/signup-disabled.test.ts 'frontend/src/app/[locale]/(auth)/actions.ts' 'frontend/src/app/[locale]/(auth)/register/page.tsx' frontend/src/features/auth/components/register-form.tsx frontend/middleware.ts 'frontend/src/app/api/proxy/[...path]/route.ts'
git commit -m "feat: disable self-service signup"
```

## Task 4: Separate process liveness from database readiness

**Files:**
- Create: `backend_py/tests/test_readiness.py`
- Modify: `backend_py/src/bfx_funding_bot/main.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/api/deps.py`

**Interfaces:**
- `GET /health` always returns `200 {"status":"ok"}` while the process is serving requests.
- `GET /ready` returns `200 {"status":"ready","checks":{"database":"ok"}}` only when `app.state.session_factory` exists and `SELECT 1` succeeds; it returns 503 with `{ "status":"not_ready", "checks":{"database":"failed"} }` for missing or failed DB state.
- `/ready` must close its session and never commit application data. It may use a short timeout from `BFX_READINESS_TIMEOUT_SECONDS` (default 2.0).

- [ ] **Step 1: Write failing HTTP tests**

Build the FastAPI app with lifespan disabled, set `app.state.session_factory` to `None`, a fake successful factory, and a fake raising factory; assert the three response contracts and that `/health` remains 200 in all cases.

- [ ] **Step 2: Run readiness tests and verify failure**

Run: `cd backend_py && uv run pytest tests/test_readiness.py -q`

Expected: FAIL because `/ready` is absent.

- [ ] **Step 3: Implement the readiness probe**

Use `sqlalchemy.text("SELECT 1")` through an async session from `app.state.session_factory`; catch `SQLAlchemyError`, `TimeoutError`, and `OSError` into the stable 503 response. Do not catch `BaseException`, and do not change lifespan’s decision to keep `/health` alive after startup failure.

- [ ] **Step 4: Run backend quality gates**

Run: `cd backend_py && uv run pytest tests/test_readiness.py tests/modules/marketfeed/test_readiness.py -q && uv run pytest -m "not integration" -q && uv run mypy src/bfx_funding_bot/main.py src/bfx_funding_bot/modules/api/deps.py && uv run ruff check src/bfx_funding_bot/main.py src/bfx_funding_bot/modules/api/deps.py`

Expected: PASS.

- [ ] **Step 5: Document and commit Release 0**

Update `backend_py/ARCHITECTURE.md` with the exact `/health`/`/ready` semantics and operator-only boundary, update both env examples, then run `git diff --check` and commit:

```bash
git add backend_py/src/bfx_funding_bot/main.py backend_py/src/bfx_funding_bot/modules/api/deps.py backend_py/tests/test_readiness.py backend_py/ARCHITECTURE.md .env.example backend_py/.env.example
git commit -m "docs: record operator containment and readiness contract"
```

## Definition of Done

- Direct signup is rejected before persistence and no `/register` UI route remains.
- Every non-public `/api/v1` route rejects non-operator principals before DB access; public routes are unchanged.
- The operator ID and admin role are explicit environment configuration; missing configuration fails closed.
- `/health` and `/ready` have separate, tested contracts.
- `cd backend_py && uv run pytest -m "not integration" -q`, `uv run mypy src/`, `uv run ruff check` and `cd frontend && pnpm lint && pnpm build` pass.
- This plan does not claim Halt 1 membership/account isolation; Plan 2 is the next gate.
