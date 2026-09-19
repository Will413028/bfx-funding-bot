# Disabled Currency Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** Bootstrap and convert a fUST-only account without inventing a disabled fUSD wallet.

**Architecture:** Enabled capital readiness is separate from disabled policy configuration. Account-wide evidence acceptance and capital authorization remain unchanged.

**Tech Stack:** Python 3.13, SQLAlchemy, pytest, PostgreSQL.

**Spec:** `docs/superpowers/specs/2026-09-20-disabled-currency-bootstrap-design.md`

## Global Constraints

- Python 3.13, pytest; no new dependencies or schema migration.
- fUST remains enabled and fully validated; fUSD remains explicitly disabled.
- Disabled conversion cells use `{"status": "disabled", "capital_evaluated": false}` only, no invented money values.
- Canonical cell IDs: fUST_a30, fUST_p2, fUSD_a30, fUSD_p2.
- No relaxation of CapitalRepository, account-wide observation, freshness, uncertainty, writer/halt or permit guards.
- No production mutation, financial API calls, authentication changes, secrets or image build by implementation agents.

### Task 1: Separate disabled configuration from enabled readiness

**Files:**
- Modify `backend_py/scripts/bootstrap_capital.py`
- Modify `backend_py/src/bfx_funding_bot/modules/accounts/capital_conversion.py`
- Test `backend_py/tests/scripts/test_bootstrap_capital.py`
- Test `backend_py/tests/modules/accounts/test_capital_conversion.py`
- Update `docs/runbooks/immutable-release.md` to document the disabled report shape and fUST-only bootstrap support.

**Interfaces:** Consume existing CapitalRepository.preview_policy unchanged. Produce unchanged bootstrap receipt and conversion digest/apply protocol, with explicitly non-financial disabled-cell report shape from Global Constraints.

- [ ] Step 1: Add failing real-database regression tests. Parameterize bootstrap ReadOnlyVenue wallets to omit fUSD while keeping its original both-wallet variant. Assert snapshot_ready, no seeded policy, original halt, no submit/cancel. Existing helper snapshot supports extra kwargs; adapt fixtures narrowly to create a genuine fUST-only immutable snapshot, never mutate classification after acceptance. Conversion must dry-run then apply the same digest and preserve disabled fUSD.

```python
assert receipt["status"] == "snapshot_ready"
assert report["symbols"]["fUSD"]["cells"]["fUSD_a30"] == {
    "status": "disabled", "capital_evaluated": False,
}
assert (await repo.read_applied(session, symbol="fUSD")).policy.enabled is False
```

- [ ] Step 2: Run focused new tests and save actual RED output. Run commands from backend_py using uv run pytest. Failure must be missing-symbol bootstrap/conversion behavior, not fixture setup/import/config error.
- [ ] Step 3: Apply narrow implementation. In bootstrap replace unconditional two-symbol preview with enabled fUST preview and canonical_cell_id("fUST", "a30"). In conversion, inside each canonical cell iteration, before calling preview:

```python
if not policy.enabled:
    values["cells"][cell] = {"status": "disabled", "capital_evaluated": False}
    continue
```

Do not change full-account recovery or repository guards. Existing enabled-symbol preview still supplies the snapshot binding in digest. Do not add catch-all fallback or wallet zero defaults.
- [ ] Step 4: Verify new tests GREEN; add negative coverage for absent enabled fUST and stale/incomplete/unknown account evidence with no policy apply. Preserve existing digest-change/idempotency tests. Actual PostgreSQL bootstrap integration must run locally, with no production env loaded. Ensure tests observe absent fUSD in stored snapshot and canonical fUST cell use. Fix pre-existing integration assertion using a30 if real report uses canonical fUST_a30.
- [ ] Step 5: Document report semantics, then run full `uv run pytest -m "not integration"`, `uv run ruff check`, `uv run mypy src/`. Run focused integration test file against local PostgreSQL fixtures. Save command/output evidence in report. Self-review and commit scoped changes with Conventional Commits only after full non-integration suite passes.

### Task 2: Controller-only fresh deployment verification

- [ ] Task/whole-branch review Task 1 before merging.
- [ ] Build once from reviewed source; transfer new immutable artifacts and independently prove target identities. No reuse/edit of old bundle metadata.
- [ ] Recheck unchanged schema/halt/permit and restricted envs; run bounded bootstrap once and inspect result. Then resume existing policy/backup/full isolated restore/halted startup runbook. If another failure occurs, diagnose rather than bypass gates. Financial activation and human authentication remain outside agent execution.
