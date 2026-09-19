# Immutable capital-policy release

This is a **human operator** procedure, not permission for a coding agent to
access production. Technical startup retains the existing durable halt. It does
not authorize a canary, cancel an offer, consume a permit, promote, or resume.
No new service, registry, signing key or hardware is required. Host/root is the
trust boundary; the application never receives a Docker socket.

## 1. Review and build once

Finish review/tests and commit first. Use a clean tracked revision; preparation
rejects tracked dirt and builds `git archive HEAD:backend_py` and `HEAD:frontend`,
not an arbitrary working directory. Untracked/ignored runtime envs cannot enter
these archives. Local integration builds are **unapproved fixtures**, not the
artifact later approved for deployment. Never rebuild an approved release on VM.

Prepare a nonsecret Docker-format env file from `deploy/vm/live.env`, adding the
explicit existing `BFX_EXCHANGE_ACCOUNT_ID` and `BFX_OPERATOR_USER_ID`. Review
every value. No inline comments, shell expansions or duplicate keys. Do not add
secrets, legacy `BFX_CANARY_*`, scalar money/cap/buffer/concentration/scope, or
`BFX_EXPECTED_IMAGE_DIGEST`. Do not reuse old `.env.runtime` as authority.
The profile is live/book_guarded with the existing fUST a30/p2 cells; applied
policy is all_available/reserve0/fixed70%, fUSD disabled with full venue coverage.
`BFX_KILL_SWITCH` remains independent break-glass, not a deployment default.

Review frontend public build inputs (example at
`deploy/vm/frontend-public.example.json`), including the actual Funnel origin.
Build-time and runtime public values must match. No secret is a build argument.

From the reviewed checkout's `backend_py/`:

```bash
uv run --frozen python -m scripts.immutable_release prepare \
  --release-id capital-policy-REVIEWED-ID --platform linux/arm64 \
  --config /absolute/reviewed/live.env \
  --frontend-public /absolute/reviewed/frontend-public.json \
  --output /absolute/new-release-directory
```

Output: `images.tar`, `bundle.json`, `manifest.json`. The bundle records archive
SHA256, source revision, exact image IDs, public build config, shared manifest
digest, inventory measurement time, and migration status **not_applied**.
`docker_image_id` is the local image/config ID; `oci_manifest_digest` is separately
nullable (usually null for a local build), never a synonym. Manifest inventory,
Python inventory, runtime environment and hashes use the candidate's shared
`release_identity` functions, schema head and projector version exactly.

The approved artifact is this once-built image pair plus protected bundle, not a
tag or a GIT_SHA label alone. Preserve its review/test/provenance receipts.

## 2. Trusted-host preparation; preserve infrastructure

Transfer only the approved nonsecret artifacts through the existing operator
channel. Install under an absolute root-controlled directory such as
`/opt/bfx/releases/REVIEWED-ID`; all ancestors root-owned, no group/world writes,
no symlinks. Protect the bundle/manifest/archive read-only. Confirm the approved
bundle digest out of band, compare `images.tar` SHA256 with `images_sha256`, then
load **that archive** with `docker load --input /absolute/images.tar`. Loading is
not rebuilding or pulling. The deployer subsequently inspects both exact IDs and
platforms; a missing/mismatched local image blocks.

Keep PostgreSQL18, Redis, container IDs, networks, volume mounts, WAL/pgBackRest
configuration, runtime files and recovery copies unchanged. Do not use broad
Compose up/build/down/remove-orphans. Compose app/migration/autoheal definitions
are now explicit `legacy-app` profile history, not this deployment path.
Quiesce all app/job/migration writers for migration and DR baseline capture;
halt alone does not quiesce reconciliation/auth writes. Confirm the existing
durable halt and no competing daemon writer. Do not silently create a halt or
an initial policy. Stop if the actual baseline differs from the reviewed record.

The host CLI itself must come from the reviewed commit in a protected checkout
with its frozen tooling environment. **Do not assume this already exists:**
controller's read-only VM preparation found network `bfx_default` and working
`sudo -n`, but no uv in remote/root PATH or the checked standard/local locations,
and no existing repository `backend_py/.venv/bin/python`.
After artifact transfer, the controller must provision protected host tooling
from the reviewed source, the candidate's uv binary and frozen Python3.13
dependencies, then verify that interpreter/dependency/source combination before
invocation. This is host tooling bootstrap, **not** an application image rebuild,
dependency upgrade, new installer/service, or permission to change the approved
artifact/runtime. Do not execute an unreviewed bootstrap download as root.

Once tooling is provisioned, invoke from its reviewed `backend_py/` as host root
using `uv run --frozen --no-sync python -m scripts.immutable_release` with that
protected uv on PATH (or its protected venv's Python directly with `-m
scripts.immutable_release`). All commands below assume this explicit prerequisite
has been completed. `EXISTING_NETWORK` is the verified `bfx_default`; recheck it
on the actual host before execution.
Do not let an unprivileged account edit that checkout, bundle or env directory.

Provision separate root:root mode0600 Docker env files without printing values:

- migration: existing owner `bfx` DATABASE_URL, **never** runtime credentials.
- bot: restricted `bfx_bot` URL, BFX_VAULT_KEK and BFX_ADMIN_TOKEN. No raw venue
  API key/secret; vault remains the source. Nonsecret settings come from manifest.
- webapi: restricted `bfx_webapi` URL, matching operator ID/admin role and the
  same BFX_ADMIN_TOKEN (server-side status reads only), existing Better Auth JWKS
  URL/issuer/audience. Internal JWKS may use `http://bfx-frontend:3000/api/auth/jwks`.
  Explicit `BFX_DEPLOYMENT_ENV=prod` must match the approved manifest realm;
  missing/mismatched values block deploy before any rename/start. It is not
  inferred from phase or injected from bot settings. The status adapter also
  independently verifies account+realm on both status and dry-run responses.
- frontend: restricted `bfx_webauth` URL, existing Redis URL/Better Auth secret,
  matching operator ID/admin role, `API_URL=http://bfx-webapi:8000`, actual public
  URLs/name from bundle, matching BETTER_AUTH_URL and PASSKEY_RP_ID. Preserve
  existing auth/session data; do not regenerate secrets or fabricate MFA flags.

Use the existing grants/migrations: bot/webapi must not own tables, inherit the
owner role, be superusers, or read `auth."user"` directly. Task4's boolean
SECURITY DEFINER function mediates operator checks. funding-status needs only
the existing account/membership read grants, not new capital/auth read grants.

## 3. Explicit schema dry-run and digest apply

Commands below use the protected bundle and existing network; replace uppercase
placeholders with reviewed absolute paths, never secret values. Each receipt
path must be new (exclusive creation, no overwrite).

```bash
uv run --frozen --no-sync python -m scripts.immutable_release migrate \
  --bundle /opt/bfx/releases/REVIEWED-ID/bundle.json --network EXISTING_NETWORK \
  --env /opt/bfx/runtime/migrate.env --receipt /opt/bfx/receipts/schema-dry.json
```

This is a **read-only migration plan**, not a pretend SQL rollback/rehearsal:
it binds database name/PG system_identifier, current heads, exact candidate
image and `uv run alembic upgrade head`. Review the digest and pending migrations,
then explicitly repeat with `--apply-digest REVIEWED_DIGEST` and a new receipt
`schema-apply.json`. A changed plan blocks. Alembic runs inside the approved
image with UV_NO_SYNC=1, RO rootfs and a bounded /tmp tmpfs; no application starts.
The post-check must report `b4e6f8a0c203`. Keep both receipts. Startup never runs
migrations implicitly. An error means inspect state; do not blindly reapply.

## 4. First-install observation bootstrap, then policy conversion

Normal daemon boot intentionally refuses absent applied policy. Conversion
intentionally refuses unavailable snapshot. On first install resolve that cycle
with the bounded one-shot below, **not** guessed wallet totals or hand-SQL policy.

```bash
uv run --frozen --no-sync python -m scripts.immutable_release bootstrap \
  --bundle /opt/bfx/releases/REVIEWED-ID/bundle.json --network EXISTING_NETWORK \
  --env /opt/bfx/runtime/bot.env --receipt /opt/bfx/receipts/bootstrap.json
```

It acquires the same account/environment WriterLock, requires an existing true
halt before loading vault credentials, runs canonical BootRecovery with complete
fUST/fUSD observations and confirmation, checks accepted freshness and unchanged
halt epoch, then exits. Default deadline60s, one recovery attempt; direct image
module `scripts.bootstrap_capital --account-id UUID --environment prod
--timeout-seconds N` accepts1–300s. It persists canonical reconciliation evidence
but constructs no executor, scheduler, financial worker or permit consumer and
calls no venue submit/cancel. Failure leaves halt and conversion guard intact.

Prepare an explicit reviewed legacy-source JSON using the unchanged conversion
contract; see `scripts.convert_capital_policy --help` and capital_conversion
source fields. It must capture effective legacy YAML caps/buffers and ignored
scalar conflicts, not silently infer authority from the old scalar0.
Required source keys are `schema_version:1`, `caps` (symbol→Decimal string),
`default_cap`, nullable `env_fallback_cap`, `buffers` (symbol→Decimal string),
`default_buffer`, nullable `env_fallback_buffer`, and `max_cell_fraction`.
Use the reviewed actual old values, not the example amounts in tests.

```bash
uv run --frozen --no-sync python -m scripts.immutable_release policy \
  --bundle /opt/bfx/releases/REVIEWED-ID/bundle.json --network EXISTING_NETWORK \
  --env /opt/bfx/runtime/migrate.env --legacy-source /opt/bfx/receipts/legacy.json \
  --receipt /opt/bfx/receipts/policy-dry.json
```

Review exact before/after string Decimals, both policies, reasons and digest.
Apply by repeating with `--apply-digest REVIEWED_DIGEST` and a new policy-apply
receipt. Snapshot maximum age300000ms; if freshness or digest changes, re-observe
and re-review rather than bypassing the unavailable check. Missing/invalid
applied policy blocks technical startup. Neither command resumes lending.

## 5. Fresh DR evidence before application writers start

Follow [offsite DR](offsite-dr.md) and
[projection audit cutover](projection-audit-cutover.md) for a fresh post-schema/
post-policy backup, quiescent source baseline, event replay/hash and isolated
restore. Keep RPO≤300s/RTO≤3600s on unchanged hardware; prior/pre-change receipts
are not acceptance. Keep existing backup/status timers and configuration intact.
The existing restore verifier resolves `bfx-bot:local`: if used, the human must
point that local tag at the approved exact ID and bind baseline to that ID; no
rebuild. Confirm weekly-report and other writers remain quiescent through capture.

Generate the existing typed Halt2 evidence (no invented fields) for this account,
image, schema, projector, canonical event prefix and config. Use the candidate's
actual `/app/configs/safety.live.yaml` as config_artifact_path and stable receipt
paths `/run/bfx-dr/backup.json`, `/run/bfx-dr/restore.json`. Do not edit hashes to
force agreement. Store the Halt2 JSON root-owned read-only. Input measured DR
receipts are root-owned mode0600 in a protected directory. Deployer copies their
bytes into the new launch evidence directory, sets runtime UID1000 mode0600 and
mounts individual files read-only at those exact paths. It runs the existing
artifact consumer in the candidate UID before starting apps. The worker still
performs the complete Halt2/continuity/freshness/DR bound checks at human action.
Existing DR measurement receipts also expire after900s. Schedule acceptance
accordingly; never change timestamps to revive stale evidence. A long restore
may require a fresh matching evidence cycle before a human session can proceed.

## 6. Technical start only

All three old app containers must exist and be stopped. Keep old containers as
recovery copies; the tool renames them with a launch-specific suffix, never
deletes them. DB/Redis must already be running on the selected existing network.

```bash
uv run --frozen --no-sync python -m scripts.immutable_release deploy \
  --bundle /opt/bfx/releases/REVIEWED-ID/bundle.json --network EXISTING_NETWORK \
  --bot-env /opt/bfx/runtime/bot.env --webapi-env /opt/bfx/runtime/webapi.env \
  --frontend-env /opt/bfx/runtime/frontend.env \
  --halt2 /opt/bfx/receipts/halt2.json --dr-directory /opt/bfx/receipts/dr
```

`scripts/deploy-vm.sh --bundle ...` is only a wrapper for this command. Phase-only
invocations fail before external commands. Deployment never pulls main/builds,
recreates PG/Redis, or clears halt. It validates principals/operator/public config,
existing policies/schema/halt, inspects created containers and publishes fresh
root-owned manifest/launch JSON **before** the exact daemon module starts:

```text
/app/.venv/bin/python -m bfx_funding_bot.modules.marketfeed.daemon
```

Runtime is UID1000, cwd/app, RO rootfs and evidence mounts, root-owned code/config/
dependencies/Python, PYTHONDONTWRITEBYTECODE=1, no Python/loader injection, no
root .env, no writable executable mounts. Image/config/container/hostname are
bound from Docker inspect, not caller claims. Every new container gets fresh
launch proof; same release/config/policy identity can retain prior promotion on
an explicitly approved restart. Bot DNS alias remains `bfx-bot`. Webapi launches
the image's Python uvicorn module; frontend starts exact saved standalone image,
only host127.0.0.1:3001 published. No automatic restart/activation is claimed.

The `technical_start_only` receipt compares PG/Redis IDs+mounts and before/after
halt/policy proof. This is **not health, MFA or lending acceptance**. Human must
check application readiness/logs (redacted), public origin/login, authenticated
API/deny paths and actual inventory/hash/account-lock timing on the unchanged
VM. Local fixture timing is not VM evidence. A partial start failure leaves
stopped recovery containers/new launch evidence for inspection; do not blindly
rerun or auto-delete/restore. Reassert/verify halt through the authorized human
incident process, inspect UNKNOWN/audit records, and reconcile before recovery.

## 7. Human authenticated workflow

Use the existing operator login and real TOTP verification. Enrollment is a
separate acceptance prerequisite: this application has verification UI but no
enrollment UI. Use the existing first-party authenticated enrollment process;
never fake twoFactorEnabled/emailVerified/role flags or put passwords/TOTP secrets
in chat. Technical frontend health does not satisfy enrollment or containment.

In Overview choose the existing account. Applied funding status shows revision,
available/unreflected/reserve/spendable/total/unattributed values as server Decimal
strings. Cell limits share one account budget; never add maxima. Disabled USD,
missing policy and durable halt are distinct. Draft settings are not applied
policy. Status failure hides money and disables preparation; no legacy fallback.

Choose strategy/period and a positive Decimal maximum plus expiry. **Prepare**
creates a durable session only. Inspect returned minimum, binding and evidence.
Human separately checks the one-off real-money confirmation and **Authorize**;
the existing daemon worker alone acts through its single-writer command path.
Poll the same session (ID is saved per account; can be restored explicitly).
After observed/consumed outcome, **Validate** requests read-only validation while
halted. Only validated state offers a separate fresh human confirmation and
**Promote**. No page load, retry, deploy, canary consumption or validation implies
promotion. Revisions changing invalidate the confirmation. On409 refresh/review,
never auto-repeat a financial authorization. UNKNOWN requires reconciliation,
not another canary/cancel retry.

Existing API contract: POST scoped `/release-sessions` with symbol/cell/strategy,
max_amount string/expires_at_ms (no caller hashes); GET `/{id}`; separate POST
`/{id}/authorize|validate|promote` with observed expected_revision. Use the existing
authenticated same-origin proxy, account membership and MFA flow; no browser
static admin token or legacy `/admin/resume`. `/funding-status` uses only a
server-side read-only daemon adapter. Operator containment, post-write rollback
decision and all final production receipts remain human obligations.

## Legacy and recovery

`canary.env`, env scope/permit/cap readers and moving-main deploy are superseded.
Historical canary YAML and evidence remain for simulations/audit, not live
authority. Legacy permit/preflight CLIs fail closed; typed preflight functions
remain in the authenticated worker. Older canary runbooks are historical evidence,
not executable instructions for this release. Never restore pre-write DB state
over post-write venue reality; follow [post-write rollback](rollback-after-venue-write.md).
