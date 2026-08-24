# Task 8 Report — Evidence-gated lending rate optimizer

## Outcome

Task 8 fix rounds 1, 2, and 3 are implemented in the `funding-execution-integrity`
worktree from base `511543d`. The original Task 8 optimizer remains pure and
executor-free. `optimizer_shadow` is observational and keeps the existing
book-guarded signal path; `optimizer_live` remains fail-closed unless the
canonical, high-confidence, exact-scope evidence and configured fee are
available. The selected live rate still crosses the existing audit-before-
`ReadyToSubmit` gate.

Fix-round 1 commit SHA: `eae8637`.
Fix-round 2 commit SHA: `9e95141`.
Fix-round 3 implementation commit SHA: `af2c028`.

## Fix-round RED

The five independent review findings were converted to regression tests before
production edits.

Initial fix-round focused run:

```text
uv run pytest -m 'not integration' \
  tests/modules/execution/deployment/test_rate_optimizer.py \
  tests/modules/execution/deployment/test_eligibility.py \
  tests/modules/execution/deployment/test_reconciler.py \
  tests/modules/marketfeed/test_config.py \
  tests/modules/marketfeed/test_daemon_wiring.py
```

Result: `17 failed, 114 passed`. The failures demonstrated the canonical
`.fill_prob` mismatch, mutable `bytearray` provenance acceptance, missing
scope arguments, collapsed `scope_mismatch/unversioned` reasons, absent live
fee validation, and missing daemon fee wiring.

The additional shadow event regression was then run before its production
change:

```text
uv run pytest -q tests/modules/execution/deployment/test_reconciler.py \
  -k canonical_reason
```

Result: `3 failed, 42 deselected`; all three canonical unavailable reasons
were missing `funding.fill_model.unavailable` telemetry.

## Fix-round 2 RED (review findings)

The fresh review findings were converted to regression tests before the
fix-round 2 production edits:

```text
uv run pytest -q \
  tests/modules/execution/deployment/test_rate_optimizer.py \
  tests/modules/execution/deployment/test_reconciler.py \
  tests/modules/observability/test_stdout_sink.py
```

Initial result: `13 failed, 58 passed`. The failures demonstrated that live
`OptimizerNoRecommendation` and optimizer errors could fall through to submit,
candidate evidence provenance was not compared against global evidence,
mutable canonical scalar fields were accepted, valid shadow optimizer results
were not observable in the expected audit shape, TAKER coverage was absent, and
the production stdout sink rejected `funding.optimizer.no_recommendation`.

## Fix-round 3 RED (critical review)

The structural-evidence live-submit regression was added before the
round-3 production edit:

```text
uv run pytest -q tests/modules/execution/deployment/test_reconciler.py \
  -k structural_evidence
```

Initial result: `1 failed, 49 deselected`. The structural object passed the
gate's attribute-based normalization, while `_optimize` returned `None`; the
reconciler therefore left the original book price eligible for live submit.

## Fix-round GREEN / verification

Final affected verification:

```text
uv run pytest -m 'not integration' \
  tests/modules/execution/deployment \
  tests/modules/execution/test_contracts.py \
  tests/modules/marketfeed/test_config.py \
  tests/modules/marketfeed/test_daemon_wiring.py
```

Result: `209 passed`.

Additional successful checks:

- Shadow canonical unavailable event regression: `3 passed, 42 deselected`.
- Fix-round 2 focused suite: `71 passed`.
- Fix-round 2 affected suite (eligibility, pricing, optimizer/reconciler,
  stdout sink, config, daemon wiring, and daemon metrics wiring): `165 passed`.
- Fix-round 3 targeted regression: `3 passed, 47 deselected` (structural
  evidence plus the existing optimizer no-recommendation/error regressions).
- Fix-round 3 affected suite (deployment, stdout sink, config, and daemon
  wiring): `221 passed`.
- Scoped mypy: `uv run mypy src/bfx_funding_bot/modules/execution/deployment` —
  success, 11 source files.
- Scoped ruff across all changed source/tests — all checks passed.
- `git diff --check` — passed.

Fix-round 3 source change: every live optimizer outcome other than
`OptimizationResult` now passes `BlockReason.OPTIMIZER_UNAVAILABLE` to the
gate, except canonical `FillModelUnavailable`, whose typed `FILL_MODEL_*`
reason remains authoritative. Structural/malformed evidence, `None`, optimizer
errors, and optimizer-local no-recommendation can therefore never fall through
to signal/book submission.

Full non-integration verification:

```text
uv run pytest -m 'not integration'
```

Result: `1850 passed, 13 failed, 85 deselected`. The remaining 13 failures
are pre-existing daemon fixtures that omit the already-required
`BFX_EXECUTION_POLICY`; they are unrelated to this fix round. The full repo
ruff check still reports six pre-existing Alembic style findings; changed-file
ruff is green.

## Changes

- `rate_optimizer.py`
  - Removed the duplicate local fill evidence/unavailable dataclasses and
    reused Task 7 canonical `modules/lending/tracking/artifact.py` types.
  - Scores canonical `.fill_prob` with the exact required formula and keeps
    optimizer-local no-recommendation semantics separate from execution
    `NoRecommendation`.
  - Recursively freezes supported JSON-safe provenance and rejects unsupported
    mutable/custom values, including nested `bytearray`; canonical evidence
    fields are validated at the optimizer boundary.
- `eligibility.py`
  - Added exact `period_agg`, `horizon_h`, model-version, and artifact-hash
    scope checks for optimizer-live evidence.
  - Preserves canonical unavailable reasons as typed execution block reasons:
    `missing`, `low_confidence`, `scope_mismatch`, and `unversioned`.
  - Deep-copies audit optimizer evidence and converts Decimal values to JSON
    strings while rejecting unsupported/non-finite values.
- `reconciler.py`
  - Calls the canonical provider `estimate_fill` seam and propagates provider
    scope into the gate; no provider injection still means no live evidence.
  - Maps only exact-period `TAKER` to taker and `UNDERCUT` to maker. `RAISE`
    and `SIGNAL_FLOOR` produce no fabricated maker candidate.
  - Preserves typed unavailable reasons in optimizer audit/telemetry and emits
    `funding.fill_model.unavailable` for every canonical unavailable reason in
    shadow mode.
  - Requires an explicit fee for optimizer-live and keeps shadow's missing-fee
    default observational only.
- Fix-round 2 `rate_optimizer.py`, `eligibility.py`, `reconciler.py`, and
  `stdout_sink.py`
  - Rejects every maker/taker candidate whose evidence differs from global
    symbol, exact period, horizon, model version, or artifact hash scope.
  - Strictly validates all canonical evidence scalar fields before freezing
    optimizer values, so runtime-injected lists/bytearrays cannot be retained
    as audit aliases.
  - Converts optimizer no-recommendation and optimizer exceptions into a typed
    live `OPTIMIZER_UNAVAILABLE` gate boundary. The gate audits the blocked
    decision first, preserves valid fill evidence, and never calls the
    executor; it does not fabricate `fill_model_missing` for valid evidence.
  - Registers `funding.optimizer.no_recommendation` in the real
    `StdoutEventSink` allowlist and retains only bounded optimizer outcome
    evidence.
- Fix-round 3 `reconciler.py` and `test_reconciler.py`
  - Adds a structural non-canonical evidence integration regression.
  - Makes the live branch fail closed for every non-`OptimizationResult`
    optimizer outcome while preserving canonical unavailable reasons.
- `config.py` and `daemon.py`
  - Require `BFX_OPTIMIZER_FEE_RATE` for `optimizer_live`; shadow may omit it.
  - Pass `config.optimizer_fee_rate` through the active daemon reconciler
    composition.
- `contracts.py`
  - Added typed `FILL_MODEL_SCOPE_MISMATCH` and `FILL_MODEL_UNVERSIONED` block
    reasons so canonical provider outcomes are not collapsed. The existing
    execution `NoRecommendation` decision-id/candidate contract is unchanged.
- Tests cover canonical provider → optimizer/reconciler/gate flow, period and
  horizon mismatches, all candidate-source branches, deep immutability and
  audit serialization boundaries, all unavailable reasons and shadow events,
  live fee/config fail-closed behavior, daemon fee wiring, and enum stability.

## Changed files

- `backend_py/src/bfx_funding_bot/modules/execution/contracts.py`
- `backend_py/src/bfx_funding_bot/modules/execution/deployment/eligibility.py`
- `backend_py/src/bfx_funding_bot/modules/execution/deployment/rate_optimizer.py`
- `backend_py/src/bfx_funding_bot/modules/execution/deployment/reconciler.py`
- `backend_py/src/bfx_funding_bot/modules/marketfeed/config.py`
- `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py`
- Corresponding deployment, contract, config, and daemon wiring tests.
- Fix-round 2 additions/changes:
  - `backend_py/src/bfx_funding_bot/modules/execution/deployment/eligibility.py`
  - `backend_py/src/bfx_funding_bot/modules/execution/deployment/rate_optimizer.py`
  - `backend_py/src/bfx_funding_bot/modules/execution/deployment/reconciler.py`
  - `backend_py/src/bfx_funding_bot/modules/observability/stdout_sink.py`
  - `backend_py/tests/modules/execution/deployment/test_rate_optimizer.py`
  - `backend_py/tests/modules/execution/deployment/test_reconciler.py`
  - `backend_py/tests/modules/observability/test_stdout_sink.py`
- Fix-round 3 additions/changes:
  - `backend_py/src/bfx_funding_bot/modules/execution/deployment/reconciler.py`
  - `backend_py/tests/modules/execution/deployment/test_reconciler.py`

## Side-effect and boundary audit

- No migration, manual SQL, remote/container/deploy action, live venue call,
  live-cap/rate change, or resume action was executed.
- No provider or optimizer is auto-created with usable evidence in the daemon;
  optimizer-live without explicit provider evidence remains blocked.
- The reconciler remains the only submit path, and optimizer-live selection is
  still audited before executor release.
- No Prometheus label was added or widened. Candidate rates, cell IDs, symbols,
  and artifact hashes remain audit JSON only; telemetry labels are bounded
  outcome/reason/policy/result values.
- The live no-recommendation path was verified with a recording executor: the
  execution decision is audited as `BLOCKED` with dependency `optimizer`, and
  the executor receives zero calls. Shadow continues to submit the existing
  exact-period book-guarded rate even when the optimizer returns a result.
- The structural-evidence regression uses a non-canonical provider object and
  verifies the same audited `BLOCKED`/zero-executor-call boundary without any
  venue or provider side effect.
- The unrelated staged deletions in the main checkout were not touched.

## Risks / residual blockers

- There is no production fill-model artifact loader/provider composition in this
  Task 8 scope. A future provider must be explicitly injected and must expose
  the canonical artifact scope; absent evidence intentionally blocks live.
- The repository-wide non-integration suite retains the 13 unrelated daemon
  fixture failures described above. Task 8 affected suites and static checks
  are green.
