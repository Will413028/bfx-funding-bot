# Halt 2 Operational RTO Target Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or **superpowers:executing-plans** to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the user-approved 3600-second operational restore target the enforced Halt 2 RTO gate so the recovery workflow can proceed without weakening its data-integrity and venue-safety checks.

**Architecture:** Define one source-level, non-runtime-overridable policy constant in the Halt 2 preflight module and test both boundary acceptance and rejection. Update every active DR/cutover spec and runbook that still claims a 60-second Halt 2 gate, while preserving the separate `archive_timeout=60s` WAL setting and the RPO, identity, replay, uncertainty, snapshot, canary, and reconcile gates.

**Tech Stack:** Python 3.13, pytest, Markdown, existing Halt 2 preflight contracts.

**Spec:** Existing DR and Halt 2 contracts in `docs/superpowers/specs/2026-09-04-offsite-dr-cloudflare-r2-design.md`, `docs/superpowers/specs/2026-09-10-projection-audit-cutover-design.md`, and `docs/runbooks/offsite-dr.md`; user-approved policy revision on 2026-09-12 aligns the Halt 2 restore gate with the already documented operational target of 3600 seconds.

## Global Constraints

- Enforce `restore_rto_seconds <= 3600`; do not make the threshold an untrusted runtime environment override.
- Keep operational `rpo_seconds <= 300` unchanged.
- Keep event-chain, projection replay/parity, account identity, schema/image/config, snapshot freshness/coverage, open-uncertainty, persistent-halt, bounded-canary, and two-reconcile gates unchanged.
- Keep `archive_timeout=60s` unchanged; it is a WAL/RPO control, not the RTO gate.
- Do not execute production migration, deploy, restart, restore, resume, or Bitfinex requests in this implementation task.
- Use pytest, never unittest; run repository-required quality checks before commit.

---

### Task 1: Align the Halt 2 policy gate and documentation

**Files:**
- Modify: `backend_py/scripts/halt2_cutover.py:192-195`
- Test: `backend_py/tests/scripts/test_halt2_cutover.py:226-256`
- Modify: `docs/runbooks/halt-2-projector-canary.md:26-47`
- Modify: `docs/runbooks/offsite-dr.md:8-18,608-623`
- Modify: `docs/superpowers/specs/2026-09-04-offsite-dr-cloudflare-r2-design.md:19-24,279-284,320-326`
- Modify: `docs/superpowers/specs/2026-09-10-projection-audit-cutover-design.md:34-36,133-140`
- Modify: `docs/superpowers/plans/2026-09-10-dr-rto-measurement.md:13-20,83-94`
- Modify: `docs/superpowers/plans/2026-08-31-halt2-canary-runbook.md:39-43`

**Interfaces:**
- Produce `HALT2_MAX_RESTORE_RTO_SECONDS: Final = 3600` in `scripts.halt2_cutover` and use it as the only Halt 2 restore-RTO comparison.
- `verify_preflight()` must accept an integer restore RTO of exactly 3600 and reject 3601 or higher with the existing bounded `restore_rto_exceeded` reason.
- All active documentation must state the same 3600-second Halt 2 gate; unrelated 60-second WAL/archive and SQL statement timeout references remain unchanged.

- [ ] **Step 1: Write the failing boundary tests**

Add a parametrized pure test around `verify_preflight()` for `restore_rto_seconds` values `60`, `61`, `3600`, and `3601`. The first three must return `EXIT_SUCCESS`; `3601` must return `EXIT_PRECONDITION_FAILED` and include only the existing `restore_rto_exceeded` reason. Change the existing exceeded fixture from `61` to `3601` so it proves the new boundary rather than the old policy.

- [ ] **Step 2: Run the focused tests and confirm the expected RED result**

Run:

```bash
cd backend_py
uv run pytest tests/scripts/test_halt2_cutover.py -q
```

Expected: the new `3600` boundary test fails because the current implementation still rejects every value above 60.

- [ ] **Step 3: Implement the minimal source change**

Import `Final`, define `HALT2_MAX_RESTORE_RTO_SECONDS: Final = 3600` beside the existing exit-code constants, and replace the literal `60` in `verify_preflight()` with that constant. Do not add a CLI flag or environment-variable override.

- [ ] **Step 4: Update normative documents**

Replace only the active Halt 2 RTO policy statements with `RTO <=3600 seconds` / `RTO ≤3600 秒`, explain that this is the approved alignment with the existing operational target, and retain all other acceptance gates. Leave `archive_timeout=60s`, `statement_timeout='60s'`, and any unrelated timing values intact.

- [ ] **Step 5: Run focused GREEN verification and stale-policy search**

Run:

```bash
cd backend_py
uv run pytest tests/scripts/test_halt2_cutover.py -q
cd ..
rg -n "Halt 2.*(RTO|restore)|restore.*RTO|rto_seconds.*60|60 秒門檻|60-second gate|stricter.*60" \
  docs backend_py/scripts backend_py/tests
```

Expected: focused tests pass; the search returns no active claim that Halt 2 requires 60 seconds. It may still return unrelated `archive_timeout=60s` or `statement_timeout='60s'` lines, which must not be changed.

- [ ] **Step 6: Run the repository quality gates**

From a fresh shell at the repository root, run:

```bash
cd backend_py
uv run pytest -m "not integration" -q
uv run mypy src/
uv run ruff check
cd ..
git diff --check
```

Expected: every command exits 0; no production or remote command is run.

- [ ] **Step 7: Commit the reviewed policy change**

```bash
git add backend_py/scripts/halt2_cutover.py backend_py/tests/scripts/test_halt2_cutover.py \
  docs/runbooks/halt-2-projector-canary.md docs/runbooks/offsite-dr.md \
  docs/superpowers/specs/2026-09-04-offsite-dr-cloudflare-r2-design.md \
  docs/superpowers/specs/2026-09-10-projection-audit-cutover-design.md \
  docs/superpowers/plans/2026-09-10-dr-rto-measurement.md \
  docs/superpowers/plans/2026-08-31-halt2-canary-runbook.md \
  docs/superpowers/plans/2026-09-12-halt2-rto-operational-target.md
git commit -m "feat(safety): align halt2 restore rto with operational target"
```

After this commit, the remaining recovery work is operator-owned: deploy the reviewed commit, generate fresh backup/snapshot/reconcile evidence, run the existing bounded canary procedure, and separately authorize any production resume. This plan does not claim those runtime gates are complete.
