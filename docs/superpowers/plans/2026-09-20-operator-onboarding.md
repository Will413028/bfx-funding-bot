# Operator Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the existing operator securely enroll TOTP and provision the first admin without bypassing financial gates.

**Architecture:** A session-only server-guarded settings route calls Better Auth directly. A separate host-only, audited bootstrap assigns the configured identity after verified enrollment and revokes stale sessions. Existing release/containment flows remain explicit.

**Tech Stack:** Next.js 16, React 19, Better Auth 1.6.14, Vitest, Node, pg/ioredis, Python pytest disposable integration fixtures.

**Spec:** docs/superpowers/specs/2026-09-20-operator-onboarding-design.md

## Global Constraints

- Retain durable halt and every financial release gate. No human secret enters agent chat or logs.
- Use pinned Better Auth 1.6.14; pnpm major 10 as Dockerfile; no production env in tests.
- Only a local QR dependency with an exact pin may be added; no auth upgrades.
- No production bootstrap apply until verified human enrollment exists.
- No localStorage, query-cache, URLs, telemetry, raw error messages or logs for enrollment payloads.
- Do not serialize the session token to the client.
- No new HTTP admin promotion route, global Redis flush, auth verification flag fabrication or financial writes.

### Task 1: Authenticated enrollment and recovery UI

**Files:**
- Create frontend/src/app/[locale]/(dashboard)/settings/security/page.tsx
- Create frontend/src/features/settings/components/two-factor-enrollment.tsx
- Create frontend/src/lib/operator-enrollment.ts with server-side identity helper and tests
- Modify frontend/src/app/[locale]/(dashboard)/settings/page.tsx and navigation if needed
- Modify frontend/src/features/auth/components/two-factor-form.tsx and its tests
- Add enrollment component and route-access tests under matching __tests__
- Modify frontend/messages/en.json, zh-TW.json, package.json/pnpm-lock.yaml only for local QR
- Add a real Better Auth enrollment integration test using its test adapter or existing isolated fixtures

**Interfaces:**
- Consumes auth.api.getSession({headers,query:{disableCookieCache:true}}), configured operator ID.
- Produces guarded /settings/security; client receives only boolean enrollment status, never session tokens.
- Enrollment uses authClient.twoFactor.enable({password}), verifyTotp({code,trustDevice:false}).
- Recovery uses authClient.twoFactor.verifyBackupCode({code,trustDevice:false}).
- Does not write any role. Task2 consumes independently verified DB enrollment state.

- [ ] Step 1: Write RED access/UI tests. Example assertions:

```tsx
expect(screen.getByRole("link", {name: /security/i})).toBeDefined();
// Render security independently of /me failure, then submit current password.
expect(enable).toHaveBeenCalledWith({password: "fixture-password"});
expect(verifyTotp).not.toHaveBeenCalled();
// Invalid code leaves the setup active; success clears the QR/codes after saved acknowledgment.
expect(screen.queryByText("fixture-backup-code")).toBeNull();
```

Cover anonymous/nonoperator/missing config/banned denial; enrolled status; password failure;
six digits including leading zero; verification failure; saved-code acknowledgment;
cancel during an in-flight request; no secret reappearance; real SVG QR from local dependency.
Use mocked external SDK only for component transport boundaries, not the enrollment state machine.

- [ ] Step 2: Run focused tests with node node_modules/vitest/vitest.mjs run <test paths>; record expected failures.
- [ ] Step 3: Implement page/helper and a small explicit state machine:

```ts
type EnrollmentStep = "password" | "verify" | "complete";
// Fresh authoritative session; exact configured ID and banned check.
// await authClient.twoFactor.enable({ password });
// retain URI/codes in component state only, render QR locally.
// await authClient.twoFactor.verifyTotp({ code, trustDevice: false });
// await authClient.getSession({ query: { disableCookieCache: true } });
// complete only after observed enrollment, no manual marker creation.
```

Extract only focused subcomponents if size requires; translations for all visible strings.
Preserve existing safe callback behavior for TOTP and new backup-code verification.
Add a settings security link before any /me early return.

- [ ] Step 4: Exercise real pinned Better Auth initial enrollment and failed verification,
including whether rotated session gets existing server-owned MFA proof. If lifecycle
does not provide execution proof, require fresh sign-in+TOTP; never manufacture it.
Run focused tests, full frontend suite, TypeScript/Biome, build with documented nonsecret placeholders.
- [ ] Step 5: Run backend_py uv run pytest -m "not integration" once before commit; commit feat(auth): add operator two-factor enrollment.

### Task 2: Audited first-admin bootstrap

**Files:**
- Create frontend/scripts/bootstrap-operator.mjs and focused helper if needed
- Create frontend/src/lib/__tests__/bootstrap-operator.test.ts
- Add backend_py/tests/integration/test_operator_bootstrap.py using existing disposable PostgreSQL fixtures plus isolated Redis if available
- Modify frontend/package.json for auth:bootstrap-operator
- Modify docs/runbooks/release-0-operator-containment.md and docs/runbooks/immutable-release.md
- Modify frontend Dockerfile tracing only if standalone dependencies are not already packaged

**Interfaces:**
- Consumes DATABASE_URL, REDIS_URL, BFX_OPERATOR_USER_ID, BFX_OPERATOR_ROLE=admin.
- CLI default --dry-run; --apply requires --confirm-bootstrap-admin and --audit-file PATH;
  optional --operator-id must equal configured ID, never selects by email.
- Produces redacted JSON audit with status, operatorUserId, dbCommitted, revoked counts;
  nonzero exit on incomplete/ambiguous state. Export main(argv, env, dependencies)
  if useful for behavior tests; command must not run on import.
- Existing revoke-non-operators.mjs runs separately after bootstrap completes.

- [ ] Step 1: Add RED behavior tests with explicit no-write/commit assertions:

```ts
expect(await run(["--dry-run"], fixture)).toMatchObject({dbCommitted:false});
expect(fixture.changedRoles()).toEqual([]);
// No verified enrollment or another admin => reject before UPDATE.
// Existing audit path => reject before DB mutation.
// Redis failure after commit => partial_failure, retry same sole admin => completed.
```

Cover wrong ID/missing env, banned/missing/ambiguous user, credential absent,
twoFactorEnabled false, verified TOTP absent/duplicate, comma-separated admin conflict,
idempotency, malformed session inventory, audit reserve failure and final-write failure.

- [ ] Step 2: Run focused tests and record expected RED failures.
- [ ] Step 3: Implement parameterized, bounded transaction and exact target validation:

```sql
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
LOCK TABLE auth."user" IN SHARE ROW EXCLUSIVE MODE;
SELECT id, role, banned, "twoFactorEnabled" FROM auth."user" ORDER BY id;
-- Validate configured target and exact verified twoFactor/credential account in transaction.
UPDATE auth."user" SET role = 'admin', "updatedAt" = CURRENT_TIMESTAMP WHERE id = $1;
COMMIT;
```

Dry run uses READ ONLY and ROLLBACK, no lock/update. Apply reserves exclusive mode600 audit
before BEGIN; logs no secrets. Postcommit delete only tokens resolved from operator
active-sessions inventory, its list and exact bfx:mfa-verified markers. No scanning
or deletion of unrelated keys. Preserve failure receipt and safe retry semantics.

- [ ] Step 4: Real disposable PG/Redis integration verifies actual role update, no authflag
changes, conflicting admin rollback, default dry no mutations, unrelated session retained.
Node CLI runs against fixture URLs only, no repo .env. Test frontend standalone script imports.
- [ ] Step 5: Document exact human enrollment route, bootstrap dry/apply, sign-in again,
then existing nonoperator containment, and continued halt. Full frontend suite/lint,
backend nonintegration and focused integration pass before feat(auth): add audited operator bootstrap.

### Task 3: Controller technical deployment and human handoff

**Files:** ignored release evidence only; no implementation by subagent.
**Interfaces:** Consumes reviewed commits, pinned bundle producer/deployer, existing protected runtime env;
produces immutable halted deployment and real route/negative-access evidence.

- [ ] Build from clean reviewed source once; verify archives/load/actual host identity.
- [ ] Follow immutable-release runbook with fresh matching evidence; no mutable hotpatch.
- [ ] Verify anonymous security route denied, login healthy, API and bot healthy, halt unchanged.
- [ ] Handoff exact /en/settings/security URL for human scan and verification. Do not access
human password/QR/backup codes or simulate their enrollment. Bootstrap apply remains pending
until actual DB verified enrollment; no lending activation in this plan.
