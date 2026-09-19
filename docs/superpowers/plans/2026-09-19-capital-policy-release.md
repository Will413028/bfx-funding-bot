# Capital Policy and Release Canary Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development task-by-task. User authorized autonomous implementation and technical deployment; no further routine approval pauses.

**Goal:** Implement the approved dynamic capital policy, separate release validation from normal live execution, and deploy a verified halted application ready for operator activation.

**Architecture:** Pure CapitalPolicy evaluates canonical per-symbol state. Durable account-scoped applied revisions, commitments and the existing command gate enforce that result. Release validation reuses the existing one-shot permit and an immutable build manifest; normal live operation does not depend on a canary cap.

**Tech Stack:** Python 3.13, Decimal, Pydantic, SQLAlchemy/PostgreSQL, Alembic, pytest, Next.js, Docker Compose on the existing VM.

**Spec:** `docs/superpowers/specs/2026-09-19-capital-policy-and-release-canary-design.md`

## Global Constraints

- Existing account, fUST only; fUSD disabled but full-account coverage preserved. No transfer or additional hardware.
- `allocation_mode=all_available`, `reserve_amount=0` on explicitly created initial policy, `max_cell_fraction=0.70`.
- Absent or invalid policy is blocked, never implicitly defaulted. Decimal native currency arithmetic; no NaN/Infinity/negative amounts.
- Preserve persistent halt, UNKNOWN fail-closed, once-only submits, account/realm isolation, audit-before-submit, single writer, existing book/strategy/loss safeguards.
- Build and deploy the same artifact, no moving-main pull/rebuild at deployment.
- RPO ≤300s and isolated-restore RTO ≤3600s remain recovery requirements.
- Agent may implement and technically deploy with halt retained. Agent must not submit real-money lending, consume a live permit, or resume autonomous lending. Real-money activation is a human operation.
- Worktree `/Users/will/second-brain/.projects/startup/bfx-funding-bot/.worktrees/capital-policy-release`; user changes in original checkout are out of scope.
- Unit tests use pytest, not unittest. Run affected tests while iterating and full non-integration suite before each implementation commit. Run actual PostgreSQL integration for database behavior.

## Task 1: Typed pure capital policy

**Files:** create `backend_py/src/bfx_funding_bot/modules/execution/capital_policy.py`, `backend_py/tests/modules/execution/test_capital_policy.py`.

**Produces:** immutable `CapitalPolicy(enabled, reserve_amount, allocation_mode, max_cell_fraction)`, `CapitalSnapshot(available_amount, unreflected_commitments, total_capital, cell_exposure)`, `CapitalBudget(spendable, cell_limit, cell_headroom, max_new_offer, reason)`, and `evaluate_capital(policy, snapshot) -> CapitalBudget`. `CapitalPolicy` defaults reserve=Decimal('0'), mode='all_available', concentration=Decimal('0.70'); enabled is required. Invalid construction raises ValueError. Policy is the explicit valid input, not a missing-data fallback.

- [ ] RED: tests with hand-derived values and missing module fail; tables cover invalid quantities, disabled, reserve exceeding assets, exhausted cell headroom, all-available deposits, Decimal precision.

```python
budget = evaluate_capital(
    CapitalPolicy(enabled=True, reserve_amount=Decimal('100')),
    CapitalSnapshot(available_amount=Decimal('1000'), unreflected_commitments=Decimal('200'),
                    total_capital=Decimal('1200'), cell_exposure=Decimal('100')),
)
assert budget.spendable == Decimal('700')
assert budget.cell_limit == Decimal('770')
assert budget.max_new_offer == Decimal('670')
```

- [ ] GREEN: implement pure arithmetic `max(0,A-L-R)`, `max(0,T-R)*fraction`, `max(0,cell_limit-E)` and their minimum. Reject unsupported mode, non-bool enabled, invalid numeric inputs and inconsistent snapshots (available > total). Return stable blocked reasons, zero budget for disabled/exhausted states. No DB/network/magic total cap.
- [ ] Verify: `cd backend_py && uv run pytest tests/modules/execution/test_capital_policy.py -q`; mypy/ruff on new module; full non-integration baseline before commit.
- [ ] Commit: `feat(capital): add typed dynamic capital budget evaluator`.

## Task 2: Applied policy, canonical snapshots and durable commitments

**Files:** create `backend_py/src/bfx_funding_bot/modules/execution/capital_repository.py`, associated `capital_tables.py`, an Alembic migration at the actual single head, and `backend_py/tests/integration/test_capital_repository.py`. Modify ORM registration, `modules/accounts/config_service.py` only where needed to validate draft-to-applied conversion. Do not change daemon or deployment yet.

**Consumes:** Task 1 types. **Produces:** account/environment/symbol-scoped applied revision reader and transaction-based authorize/reserve operation with compare-on-revision/snapshot fence. Exact signatures are defined in repository code and reported for Task 3. Immutable revision rows plus active-pointer/version ensure applied is distinct from draft. Commitment state must connect to existing immutable attempt/intent identity, not become a parallel untraceable ledger.

- [ ] RED: PostgreSQL tests create two independent sessions competing for the same remaining funds and assert only one reservation fits. Include policy apply CAS, unavailable policy, account/realm isolation, snapshot already-reflected vs unreflected classification, crash/restart recovery.

```python
# Given 700 spendable, two simultaneous 500 reservations:
assert sorted(outcomes) == ['blocked', 'reserved']
assert durable_pending == Decimal('500')
# A snapshot proving a 200 commitment reflected must not charge it twice.
assert before_snapshot_budget == after_snapshot_budget == Decimal('700')
```

- [ ] GREEN: use existing projection/attempt/event identities to derive canonical capital. Lock account policy/state consistently; revision, snapshot and reservation writes occur in one transaction. Reject unclassifiable commitments/UNKNOWN. Persist metadata needed to prove snapshot inclusion; do not use timestamps alone as inclusion proof. Retain reservations until confirmed terminal outcome/reflection. Never hold DB transaction across venue I/O.
- [ ] Add dry-run policy conversion CLI using existing account UUID and an explicit apply command: show old values/new policy/exposure delta, preserve drafts/history, report invalid legacy settings. New all_available mode never implies resume. Alembic migration does not silently authorize production policy.
- [ ] Verify actual PostgreSQL migration, concurrency and replay tests; unit suite and mypy/ruff. Commit bounded repository changes.

## Task 3: One policy across planner, submit and status; normal live mode

**Files:** modify `modules/execution/deployment/reconciler.py`, `deployment/sizing.py`, `modules/execution/command_gate.py`, `modules/execution/safety/hard_guards.py`, `modules/marketfeed/daemon.py`, config Phase enum/validation, account context contracts and trading status consumers. Add affected behavior tests under corresponding existing test directories.

**Consumes:** Tasks 1–2 policy/snapshot/reservation contracts. **Produces:** normal `live` boot path; planner + authoritative submit evaluation + status share one evaluator and applied revision. Existing paper/shadow retained. Release one-shot is handled in Task 4, never needed for normal daemon startup.

- [ ] RED: exercise normal live with two configured cells and no canary env. Stale revision/snapshot, changed available and simultaneous submit must prevent external calls; missing policy blocks live boot. Unrelated existing filters still run.

```python
assert status.policy_revision == planned.policy_revision
assert status.max_new_offer == planned.capital_budget.max_new_offer
# With stale revision, a queued READY decision cannot reach the venue.
assert outcome.reason == 'capital_policy_revision_changed'
assert venue_received == []
```

- [ ] GREEN: remove allocation scalar/env/map fallback from live code; load validated applied policy. Preserve DB single-writer fencing and durable pre-send reservation. Reserve/size decisions account for already-frozen offers exactly once, keep UNKNOWN blocked and prohibit early cancellation reuse. Cell allocation uses total capital while new spending uses remaining spendable.
- [ ] Replace live guard phase checks so moving from canary to live cannot disable existing required guards. Reject old live money env at boot with actionable migration error. Update old simulated tests to explicit policies rather than introduce hidden legacy compatibility.
- [ ] Verify affected planner/command/status/daemon tests, PostgreSQL fault/concurrency cases, full non-integration suite and mypy/ruff. Commit integrated policy path.

## Task 4: Release session and once-only canary lifecycle

**Files:** modify `modules/execution/canary_permit.py`, `uncertainty_tables.py`, related canary scripts/tests; create focused release session module/table migration, extend existing authenticated operator control path for preparation/validation/promotion. No live execution by agents.

**Consumes:** canonical account and CapitalPolicy; normal live executor guarded by existing AccountCommandGate. **Produces:** prepared/authorized/consumed/observed/validated/promoted session with artifact/config/policy/schema/projector/halt/scope binding, max_amount and explicit expiry; typed receipts.

- [ ] RED: expired or mismatched artifact/policy/session cannot consume; two sessions on same halt epoch still cannot submit twice. Normal configured cells need not equal one-shot selected cell. Crashes after consumption never retry. Mock venue test verifies maximum one submit.

```python
assert first.state == 'consumed'
assert second.reason == 'permit_already_consumed'
assert simulated_venue_calls == 1
assert persistent_halt is True
```

- [ ] GREEN: reuse permit uniqueness/consumption and bind exact amount before venue call. Canary max_amount is an additional bound, never equality with normal budget. Validate fresh ACK-derived evidence and two post-outcome reconcile fences. Existing RPO/RTO/coverage/schema gates remain.
- [ ] Promotion code rechecks authorization, current runtime hashes, epoch and readiness in audited transaction; validated alone does not resume. Failure reasserts halt; failed halt persistence stops writer. Expiry stops new submit, not observation.
- [ ] Verify PostgreSQL consumption/epoch races, stale evidence, UNKNOWN and rollback tests, full unit suite/type/lint. Commit lifecycle change.

## Task 5: Immutable release packaging, migration UX and application display

**Files:** `scripts/deploy-vm.sh`, new release manifest tooling with behavior tests under `backend_py/tests/scripts/`, `deploy/vm/live.env`, Compose as needed, frontend funding/status view and account configuration types, `backend_py/ARCHITECTURE.md`, operator runbooks/examples. Replace stale untracked wizard only in isolated worktree if needed; never include user's original untracked files.

**Produces:** prepare/build once → verified manifest → deploy same image. Digest comes from artifact; image ID and OCI manifest digest remain distinct types. Schema/policy migrations have explicit dry-run/apply receipts, no live fallback.

- [ ] RED: a fake Docker command runner checks deploy does not build/pull main, rejects mismatched image/platform/config, and defaults to halted startup. Backend/frontend agree on available/pending/reserve/headroom and applied revision.

```python
assert deployment.build_invocations == 0
assert deployment.started_image_id == release.approved_image_id
assert deployment.halt_preserved is True
```

- [ ] GREEN: artifact preparation emits manifest and migration report; deployment loads protected secrets without printing and checks immutable inputs before startup. Remove superseded canary profile, duplicated amount/cap/scope env and runtime readers. Docs preserve historical evidence, mark obsolete commands, show human-only canary/promotion operation.
- [ ] UI exposes meaningful policy state, reserve and why lending is blocked; does not ask users for technical env identifiers. Existing operator login/TOTP status is an acceptance dependency; do not fake enrollment or print secrets.
- [ ] Verify command behavior with controlled fixtures, frontend tests/build, complete backend checks. Commit release workflow and UX.

## Task 6: Whole-branch review, merge and halted deployment acceptance

**Files:** release acceptance report under `docs/reports/2026-09-19-capital-policy-release.md`, fixes limited to findings and regressions.

- [ ] Read-only whole-branch spec/quality review; fix real findings and rerun affected tests.
- [ ] Run final unit/integration/mypy/ruff/frontend checks, Alembic schema drift, artifact consistency. Only after passing merge local main without touching unrelated modifications; command-scope correct GitHub identity if pushing is required by delivery.
- [ ] Recheck VM image, DB schema/backup/timers/halt, stage exact artifact, use protected credentials, apply reviewed non-trading schema changes and start technical services with halt retained. Preserve recoverable rollback artifact and original config.
- [ ] Verify FE HTTP/login, webapi health, DB/Redis health, bot readiness reports correct policy revision and explicit halt rather than crash-loop. Verify no new venue writes and fresh account reconciliation when available.
- [ ] Record actual deployed hashes, tests and outstanding acceptance dependencies. Human-only activation command remains unexecuted; never claim lending started without evidence or bypass TOTP/financial-operation boundary.
