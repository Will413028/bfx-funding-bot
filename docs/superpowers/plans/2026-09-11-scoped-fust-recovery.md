# Scoped fUST recovery implementation plan

> **For Codex:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task.

**Goal:** Resume the halted production bot with a narrowly scoped fUST-only projection cutover, keep fUSD dark, and make the configured lending amount equal to available fUST unless an explicit reserve/buffer is configured.

**Scope:** This plan supersedes the dual-symbol venue snapshot requirement for this recovery only. It does not transfer funds. It retains archive, isolated restore verification, atomic event-only replay, quiescence, and post-cutover canary gates.

**Design:** `--managed-symbols fUST` is the explicit operator scope. The prepared artifact binds the selected managed-symbol set; apply derives and validates the same scope from the pinned snapshot, while allowing the normal post-prepare snapshot refresh to have a new event identity. A missing or substituted symbol cannot be inferred as zero. The runtime parity verifier checks only the selected scope while rejecting any out-of-scope active exposure or nonzero position; an all-zero supported-symbol row is treated as an inert legacy scaffold and is removed by the atomic rebuild. The canary configuration removes fUSD cells, keeps an explicit fUSD cap of zero, and sets fUST buffer to zero. The default code fallback remains unchanged outside the canary profile unless separately configured.

## Task 1: Add failing tests for explicit scoped cutover identity

**Files:**
- Modify: `backend_py/tests/scripts/test_cutover_projection.py`
- Modify: `backend_py/tests/integration/test_projection_cutover_apply_v2.py`
- Modify: `backend_py/tests/modules/execution/projection_cutover/test_snapshot.py`

- Add parser coverage for `--managed-symbols fUST`, invalid/duplicate symbols, and the compatibility default.
- Add a collection test proving a wallet response containing only explicit fUST evidence is accepted when the requested scope is fUST, while the existing dual-symbol test continues to reject missing fUSD.
- Add apply/CLI artifact-binding coverage proving the managed symbol scope is recorded in the prepared artifact and cannot drift during apply; allow the normal post-prepare snapshot refresh to carry a new event identity.
- Add an integration case proving fUST-only parity succeeds with no fUSD position/exposure and that an out-of-scope fUSD projection fails closed.
- Run the focused tests and observe the expected failures before changing production code.

## Task 2: Implement managed-symbol scope through prepare and apply

**Files:**
- Modify: `backend_py/scripts/cutover_projection.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/projection_cutover/apply.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/projection_cutover/snapshot.py`

- Parse and validate a bounded supported-symbol set, defaulting to the existing dual-symbol behavior for callers that do not opt into the new scope.
- Pass the parsed set into snapshot collection and preparation instead of hardcoding both symbols.
- Record the managed symbol set in the prepared artifact, require the apply CLI scope and snapshot wallet keys to match it, and derive the runtime managed set from the pinned snapshot.
- Replace hardcoded dual-symbol exposure calculations with the pinned managed set.
- Require all live active offer/credit exposure and position rows to be within the managed set; this keeps fUSD dark fail-closed rather than silently trusting omitted data.
- Preserve the existing archive scope and event-only full rebuild so historical data remains archived and the new baseline is derived from the immutable event log.
- Run focused unit tests, then the relevant integration tests.

## Task 3: Make canary fUSD dark and reserve behavior explicit

**Files:**
- Modify: `backend_py/configs/cells.canary.yaml`
- Modify: `backend_py/configs/safety.canary.yaml`
- Modify: `deploy/vm/canary.env`
- Modify: relevant canary/deploy config tests under `backend_py/tests/modules/marketfeed/`

- Remove fUSD canary cells so no fUSD strategy work is scheduled.
- Set the fUSD cap to zero as a defense-in-depth policy and retain only the fUST armed cap.
- Set the fUST buffer to zero and explicitly set `BFX_BALANCE_BUFFER_USDT=0`, documenting that a nonzero reserve must be configured deliberately.
- Verify the deployed profile resolves only fUST and zero buffer before building the VM image.

## Task 4: Review, build, and execute the production cutover

**Files/artifacts:**
- Update the project decision/recording notes after the operational result is known.
- Do not commit private VM evidence, credentials, or the protected untracked test file.

- Run the full non-integration pytest suite from `backend_py/` plus targeted static checks.
- Request a code review of the diff and resolve findings.
- Commit with a Conventional Commit and push to the personal remote if verification is green.
- Pull/build the pinned VM release without starting the halted app writers; refresh the operations inventory and evidence.
- Rerun source identity migration/quiescence checks, collect a fresh fUST-only venue snapshot, perform R2 backup/preflight and isolated restore verification, then prepare and apply the archive/cutover transaction.
- Verify the new event/projection baseline, keep fUSD dark, verify buffer/cap/cell configuration, and only then perform the smallest canary resume gate. If any gate fails, keep the trading halt persisted and report the exact blocker.

## Verification commands

- `cd backend_py && uv run pytest -m "not integration"`
- `cd backend_py && uv run mypy src/ && uv run ruff check`
- Relevant integration tests with the repository's PostgreSQL 18 fixture when available.
- VM-side release/preflight, R2 smoke, isolated restore, projection cutover, and post-resume health checks; record only redacted digests and statuses.
