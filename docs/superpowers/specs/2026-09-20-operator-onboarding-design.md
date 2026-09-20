# Operator onboarding: authenticated TOTP and controlled first admin

## Approved intent

The operator authorized implementing, testing and deploying the missing security
settings and first-admin workflow, and replied to proceed after its design was
presented. This work does not activate lending. Retain durable halt and every
financial release gate. No human secret enters agent chat or logs.

## Problem and choice

Better Auth 1.6.14 already provides enable/verify TOTP; the application only
exposes sign-in verification. Settings depends on MFA-gated backend /me, creating
a setup deadlock. There is no audited first-admin bootstrap tool.

Use first-party security settings backed by Better Auth's existing session/API,
then an explicit host-only bootstrap after verified enrollment. Do not use browser
console snippets (poor repeatability), self-service promotion (new escalation
surface), or fabricated authentication flags. QR encoding runs locally in the
browser; no external QR endpoint receives the secret.

## Security settings

Add localized /settings/security under the existing dashboard. It must work for
a password-authenticated operator before backend authorization/admin provisioning.
Use a fresh server-side Better Auth session lookup with cookie cache disabled and
require exact configured BFX_OPERATOR_USER_ID, non-banned user; no role or MFA
prerequisite to access enrollment. Anonymous and other users fail closed.
Do not serialize the session token to the client. Add a navigation link reachable
even when Settings /me fails. Do not route enrollment through Python/BFF.

Call the pinned SDK twoFactor.enable with current password; render totpURI as a
local SVG QR and show backup codes only inside ephemeral component state. Confirm
saved backup codes, then verify a six-digit TOTP with trustDevice=false. Only real
verification plus authoritative refreshed session state reports enrolled. Never set
twoFactorEnabled, verified, emailVerified or MFA markers manually. Password/code
are cleared after use, secrets are cleared on cancel/unmount/completion, and
async results after leaving the flow must not redisplay secrets. No localStorage,
query-cache, URLs, telemetry, raw error messages or logs for enrollment payloads.
Retain secret material only until the user acknowledges saving backup codes.
Show safe translated errors and retry without accidentally regenerating secrets.
Already-enrolled users see status, not an enable/reset/disable action. Re-enrollment,
recovery reset and passkey setup are not in scope.

Keep existing /two-factor login verification. Add a backup-code login alternative
so the generated recovery codes have a usable recovery path, using the pinned
SDK and the same safe redirect logic. Do not enable trusted-device shortcuts.
Production JWT minting remains server-only and requires the existing exact-session
MFA proof; add regression coverage around initial enrollment and failed verification.

## Controlled admin bootstrap

Add a host-only Node CLI beside revoke-non-operators.mjs. It is not an HTTP route.
Mandatory configured immutable operator ID and role=admin; CLI ID must match env.
Read-only dry-run is default. Apply requires explicit --apply,
--confirm-bootstrap-admin, and a new exclusive private audit path. Under a bounded
transaction/table lock, require the exact existing non-banned user, credential
account, twoFactorEnabled=true and exactly one verified TOTP row. Refuse other
admins (including comma-separated admin roles) or ambiguous target state.
Assign role=admin only to that user, without modifying auth verification flags or
any financial tables. A repeat with the same sole admin is safe/idempotent.
Reserve/write private audit evidence before mutation; redact secrets and emit
bounded failure codes. After commit, revoke only the operator's known Redis
sessions/list/MFA markers so cached user roles cannot persist; no global flush,
prefix-wide deletion, other user deletion or token output. Partial cleanup failure
is explicit and retryable; no success without a completed audit. Existing
non-operator containment remains a separate reviewed tool after bootstrap.

## Verification and deployment

Use TDD: UI tests cover access/error/cancel/verification/recovery; real Better Auth
integration covers initial enrollment session rotation and failed verification;
CLI tests cover default no writes, identity/mfa/admin conflicts, audit failures,
transaction rollback and retry; actual disposable PostgreSQL/Redis prove effects.
Use pinned Better Auth 1.6.14; pnpm major 10 as Dockerfile; no production env in tests.
Only a local QR dependency with an exact pin may be added; no auth upgrades.
Run frontend suite/lint/build, backend pytest non-integration before commits,
independent review and immutable release validation. Do not hotpatch running images.
Deploy technical services while halted. Human scans/verifies only after deployment.
No production bootstrap apply until verified human enrollment exists. Actual
financial activation remains outside this change; refresh DR evidence separately.

## References

- Pinned installed Better Auth 1.6.14 two-factor index/totp/verify-two-factor modules.
- https://better-auth.com/docs/1.6/plugins/2fa (checked 2026-09-20).
- docs/runbooks/immutable-release.md and release-0-operator-containment.md.
