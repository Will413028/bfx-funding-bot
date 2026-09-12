# Halt 2 Migration, Fault Injection and Bounded Canary Runbook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Plan 2–4 完成後，以可重放 projection、可注入 venue faults、明確 halt/rollback 邊界與一筆最小 real-money canary，驗證 serialized projector、UNKNOWN、quarantine 與 account identity 能安全取代舊 execution image。

**Architecture:** Halt 2 是 clean cutover，不做 runtime dual-read。先在持久 halt 下建置 event identity/projector/uncertainty schema，將 empty projections 由 event_log replay 重建並與舊 diagnostic projection 比對；再把 unresolved PENDING 轉成 UNKNOWN、對 venue orphan quarantine；只有一個最小 command 通過 guards、durable outcome 與兩個 full-account reconcile cycle 後，才允許 operator 依 runbook 判定可恢復。Venue 已可能寫入後，rollback 只能 halt + reconcile + adopt/resolve/forward-fix，不能直接 DB restore。

**Tech Stack:** Python 3.13、pytest、PostgreSQL、Alembic、Docker Compose、Bash、Next.js 16、Markdown runbooks、structured JSON reports。

**Spec:** docs/superpowers/specs/2026-08-31-production-integrity-foundation-design.md（Halt 2、fault injection、bounded canary）。

## Global Constraints

- 本 plan 只產生本地 scripts、tests、env contract 與 runbook；不得執行 remote migration、restart、deploy、resume、cap increase 或 Bitfinex write。
- Halt 2 前必須已完成 Plan 2 identity verify、Plan 3 deterministic replay、Plan 4 UNKNOWN/orphan integration tests；任何一項未通過都保持 persistent halt。
- Projection replay 僅使用 event_log、upcasters、projector version；舊 projection 只能產生 diagnostic diff，不能作 runtime fallback 或 second source of truth。
- Fault injector 必須能模擬 accept+drop、reject+drop、timeout/reset、malformed body、5xx-after-side-effect、process crash-before-outcome、multiple-candidate 與 out-of-order delivery；每個 case 都驗證 one-attempt/no-retry。
- Canary 只允許一個預先指定 account、symbol、最小 amount、單一 strategy/cell；禁止從 canary 報告自動提升 cap 或擴大 symbols。
- Pre-cutover backup/restore evidence must record the ADR targets RPO <= 5 minutes and Halt 2 restore RTO <= 3600 seconds; an unmeasured claim is a failed gate.
- 報告不得包含 API key/secret、Authorization header、完整 raw response；只存 hash、bounded reason、event seq、venue ID 與時間。
- 所有 migration/verification commands 從 backend_py/ 使用 uv run；實際 production 操作依 runbook 由 operator 執行。

---

## File Structure

### New files

| File | Responsibility |
|---|---|
| backend_py/scripts/halt2_cutover.py | preflight, persistent halt assertion, schema/replay gate and dry-run report |
| backend_py/scripts/verify_projection_replay.py | empty-projection rebuild and stable content-hash verifier |
| backend_py/scripts/run_canary_preflight.py | bounded canary configuration and evidence checklist |
| backend_py/tests/modules/execution/test_fault_injection.py | deterministic transport/crash fault matrix |
| backend_py/tests/integration/test_halt2_replay_cutover.py | replay, PENDING conversion, quarantine and rollback boundary |
| backend_py/tests/scripts/test_halt2_cutover.py | CLI preflight and report contract |
| docs/runbooks/halt-2-projector-canary.md | operator sequence, evidence and stop conditions |
| docs/runbooks/rollback-after-venue-write.md | irreversible venue-write rollback decision tree |
| docs/reports/templates/halt2-evidence.json | redacted evidence report schema |

### Modified files

| File | Responsibility after this plan |
|---|---|
| scripts/deploy-vm.sh | refuses cutover/canary without account UUID, operator ID, backup/replay evidence and explicit confirmation |
| deploy/vm/paper.env | explicit identity/projector settings for simulated phase |
| deploy/vm/shadow.env | explicit identity/projector settings and no implicit realm |
| deploy/vm/canary.env | bounded account/symbol/amount, persistent halt default and two-reconcile gate |
| deploy/vm/soak-checkpoint.sh | includes account-local projector head/lag and open uncertainty counts |
| backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py | invokes PENDING-to-UNKNOWN conversion before eligibility |
| backend_py/src/bfx_funding_bot/modules/execution/event_store/projector.py | exposes replay/hash and old-projection diagnostic comparison |
| backend_py/src/bfx_funding_bot/modules/execution/periodic_reconcile.py | exposes reconcile fence/cycle evidence for canary |
| backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py | blocks live startup until Halt 2 readiness evidence is present |
| backend_py/tests/modules/marketfeed/test_daemon.py | startup refusal and canary-bound assertions |
| backend_py/ARCHITECTURE.md | records clean-cutover and rollback invariants |

## Task 1: Build deterministic venue fault-injection harness

**Files:**
- Create: backend_py/tests/modules/execution/test_fault_injection.py
- Modify: backend_py/src/bfx_funding_bot/external/bitfinex/live_executor.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/command_gate.py

**Interfaces:**
- FaultScenario(name, response_mode, transport_started, side_effect_visible, process_crash_at) is immutable and enumerates the seven fault modes in the spec.
- FakeBitfinexTransport records request count, normalized payload hash and side-effect venue objects; it can drop the response after recording a side effect.
- run_fault_scenario(scenario) returns FaultEvidence with attempt count, outcome kind, uncertainty state, event seqs, venue object count and retry count.
- Every scenario asserts attempt count = 1; unknown/side-effect cases never call executor again automatically.

- [ ] **Step 1: Write failing fault matrix tests**

Assert accept+drop, reject+drop, timeout/reset, malformed, 5xx-after-side-effect, crash-after-intent and out-of-order reconcile outcomes. Assert no secret/header appears in FaultEvidence.

- [ ] **Step 2: Run tests and verify failure**

Run: cd backend_py && uv run pytest tests/modules/execution/test_fault_injection.py -q

Expected: FAIL because the executor still collapses transport faults to failed and there is no reusable fault harness.

- [ ] **Step 3: Implement injectable transport and evidence collector**

Keep production executor free of test branches; inject an httpx transport/fake port. Route every result through submit_outcomes and command_gate. Persist only bounded evidence fields.

- [ ] **Step 4: Run fault matrix and static checks**

Run: cd backend_py && uv run pytest tests/modules/execution/test_fault_injection.py -q && uv run mypy src/bfx_funding_bot/external/bitfinex/live_executor.py src/bfx_funding_bot/modules/execution/command_gate.py && uv run ruff check src/bfx_funding_bot/external/bitfinex/live_executor.py src/bfx_funding_bot/modules/execution/command_gate.py

Expected: PASS.

- [ ] **Step 5: Commit the harness**

~~~bash
git add backend_py/tests/modules/execution/test_fault_injection.py backend_py/src/bfx_funding_bot/external/bitfinex/live_executor.py backend_py/src/bfx_funding_bot/modules/execution/command_gate.py
git commit -m "test: add deterministic submit fault matrix"
~~~

## Task 2: Implement Halt 2 preflight and persistent-halt gate

**Files:**
- Create: backend_py/scripts/halt2_cutover.py
- Create: backend_py/tests/scripts/test_halt2_cutover.py
- Modify: scripts/deploy-vm.sh
- Modify: deploy/vm/paper.env
- Modify: deploy/vm/shadow.env
- Modify: deploy/vm/canary.env

**Interfaces:**
- halt2_cutover.py has subcommands preflight, assert-halt, replay, convert-pending, quarantine, verify and release-report; default is read-only preflight.
- PreflightReport contains migration head, schema heads, backup evidence hash, isolated-restore evidence hash, event count/head/hash per account/env, open uncertainty count, venue snapshot fence, config/image digest and stop reasons.
- assert-halt writes only through the existing durable TradingHalt event path; if DB is unavailable the command exits nonzero and deployment remains stopped.
- deploy-vm.sh requires BFX_EXCHANGE_ACCOUNT_ID, BFX_OPERATOR_USER_ID, projector version, evidence report path and BFX_CANARY_CONFIRM for canary; it rejects BFX_ACCOUNT_ID.

- [ ] **Step 1: Write failing CLI/preflight tests**

Cover missing evidence, stale event hash, open unresolved uncertainty, wrong account UUID, absent persistent halt, legacy env variable, and valid read-only report. Assert no command mutates DB in preflight mode.

- [ ] **Step 2: Run tests and verify failure**

Run: cd backend_py && uv run pytest tests/scripts/test_halt2_cutover.py -q

Expected: FAIL because the CLI and deployment gates do not exist.

- [ ] **Step 3: Implement preflight and env validation**

Use argparse with explicit subcommands, JSON report output and exit codes 0/2/3 for pass/precondition-failed/verification-failed. Read evidence via parameterized DB queries; never print credentials. Keep canary default halted.

- [ ] **Step 4: Run CLI tests and shell syntax checks**

Run: cd backend_py && uv run pytest tests/scripts/test_halt2_cutover.py -q && bash -n ../scripts/deploy-vm.sh && bash -n ../deploy/vm/soak-checkpoint.sh

Expected: PASS.

- [ ] **Step 5: Commit Halt 2 gate**

~~~bash
git add backend_py/scripts/halt2_cutover.py backend_py/tests/scripts/test_halt2_cutover.py scripts/deploy-vm.sh deploy/vm/paper.env deploy/vm/shadow.env deploy/vm/canary.env
git commit -m "feat: gate halt two on verified evidence"
~~~

## Task 3: Replay empty projections and convert unresolved state

**Files:**
- Create: backend_py/scripts/verify_projection_replay.py
- Create: backend_py/tests/integration/test_halt2_replay_cutover.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/event_store/projector.py

**Interfaces:**
- verify_projection_replay.py creates an empty projection schema, replays one account/env, emits row counts/content hashes, compares old projection only in a diagnostic report, and leaves runtime tables untouched.
- convert_pending_to_unknown(account_id, environment, now_ms) appends one SubmitOutcomeUnknown plus one uncertainty per unresolved PENDING, never invokes executor, and is idempotent.
- Replay rejects missing upcaster/projector version, invalid event identity, non-contiguous event seq or hash mismatch.
- quarantine_orphans_after_replay emits VenueOfferQuarantined for unmatched active offers and leaves persistent halt on any open uncertainty.

- [ ] **Step 1: Write failing replay/conversion tests**

Cover empty projection rebuild, stable hash across two runs, historical v2 UUID derivation, missing event seq, unresolved PENDING conversion, idempotent rerun, orphan quarantine and old/new diagnostic diff.

- [ ] **Step 2: Run integration tests and verify failure**

Run: cd backend_py && uv run pytest tests/integration/test_halt2_replay_cutover.py -m integration -q

Expected: FAIL because replay CLI, PENDING conversion and quarantine gate are absent.

- [ ] **Step 3: Implement replay and conversion**

Use a temporary schema/transaction for replay; only after hash verification may the operator run the explicit conversion subcommand against runtime tables. Keep all conversion events serialized by AccountEventWriter.

- [ ] **Step 4: Run replay and non-integration checks**

Run: cd backend_py && uv run pytest tests/integration/test_halt2_replay_cutover.py -m integration -q && uv run pytest tests/modules/execution/event_store tests/modules/execution/test_boot_recovery.py -m "not integration" -q

Expected: PASS.

- [ ] **Step 5: Commit replay/cutover conversion**

~~~bash
git add backend_py/scripts/verify_projection_replay.py backend_py/tests/integration/test_halt2_replay_cutover.py backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py backend_py/src/bfx_funding_bot/modules/execution/event_store/projector.py
git commit -m "feat: replay projections and convert pending intents"
~~~

## Task 4: Run bounded canary evidence and stop conditions

**Files:**
- Create: backend_py/scripts/run_canary_preflight.py
- Create: docs/reports/templates/halt2-evidence.json
- Modify: backend_py/src/bfx_funding_bot/modules/execution/periodic_reconcile.py
- Modify: backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py
- Modify: backend_py/tests/modules/marketfeed/test_daemon.py
- Modify: deploy/vm/canary.env
- Modify: deploy/vm/soak-checkpoint.sh

**Interfaces:**
- run_canary_preflight.py validates exactly one account UUID, one configured symbol, one cell/strategy, minimal amount <= configured cap, all hard guards enabled, fresh full-account snapshot, zero open uncertainty, replay hash and backup/restore evidence.
- CanaryEvidence records command decision ID, one attempt ID, outcome kind, venue ID if acknowledged, two reconcile fences/cycle timestamps, account-local projection hash, venue-vs-DB exposure diff and stop reason.
- Daemon refuses real-money startup when canary evidence is missing/stale, open uncertainty exists, projector lag is nonzero, venue snapshot coverage is incomplete, or identity/env mismatch exists.
- soak-checkpoint.sh is read-only and reports account-local open uncertainties, projector lag, last two reconcile fences and halt state.

- [ ] **Step 1: Write failing canary preflight/startup tests**

Cover each missing gate, one valid bounded profile, cap increase attempt, second symbol, stale evidence, projector lag, and open uncertainty. Assert no venue submit occurs when any gate fails.

- [ ] **Step 2: Run tests and verify failure**

Run: cd backend_py && uv run pytest tests/modules/marketfeed/test_daemon.py tests/scripts/test_halt2_cutover.py -q

Expected: FAIL because canary evidence and startup gate are absent.

- [ ] **Step 3: Implement bounded canary gate**

Load explicit immutable canary settings; compare account UUID and deployment environment; require two successful reconcile cycles after the one outcome. Keep persistent halt on timeout, mismatch or any new uncertainty.

- [ ] **Step 4: Run canary tests and shell checks**

Run: cd backend_py && uv run pytest tests/modules/marketfeed/test_daemon.py -q && bash -n ../deploy/vm/soak-checkpoint.sh

Expected: PASS.

- [ ] **Step 5: Commit canary evidence tooling**

~~~bash
git add backend_py/scripts/run_canary_preflight.py docs/reports/templates/halt2-evidence.json backend_py/src/bfx_funding_bot/modules/execution/periodic_reconcile.py backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py backend_py/tests/modules/marketfeed/test_daemon.py deploy/vm/canary.env deploy/vm/soak-checkpoint.sh
git commit -m "feat: enforce bounded canary evidence gates"
~~~

## Task 5: Document irreversible rollback and final verification

**Files:**
- Create: docs/runbooks/halt-2-projector-canary.md
- Create: docs/runbooks/rollback-after-venue-write.md
- Modify: backend_py/ARCHITECTURE.md
- Modify: backend_py/tests/integration/test_halt2_replay_cutover.py

**Interfaces:**
- Halt 2 runbook has preflight evidence table, exact command order, operator confirmation points, expected exit codes, stop/abort conditions and post-canary two-reconcile observation window.
- Rollback runbook distinguishes before-venue-write image rollback from after-venue-write halt/reconcile/adopt/manual-resolution/forward-fix; DB restore is allowed only with proof that no later venue mutation occurred.
- Final verification includes API auth denial, direct signup denial, account-local projection hash, event-chain replay, UNKNOWN fault matrix, orphan quarantine, fresh venue full-account diff, persistent halt effectiveness and no automatic retry.

- [ ] **Step 1: Write failing documentation/checklist tests**

Add text-based tests that require every gate name, rollback branch, command, report field and forbidden action to appear in the runbook.

- [ ] **Step 2: Run documentation tests and verify failure**

Run: cd backend_py && uv run pytest tests/integration/test_halt2_replay_cutover.py -q

Expected: FAIL because the runbooks and evidence template are incomplete.

- [ ] **Step 3: Write the operator runbooks**

Document commands exactly as they must be run from backend_py or repository root, include redacted sample JSON, and state that the agent does not execute production operations. Link Plan 2/3/4 verification artifacts.

- [ ] **Step 4: Run complete local verification**

Run: cd backend_py && uv run pytest -m "not integration" -q && uv run pytest tests/integration/test_halt2_replay_cutover.py tests/integration/test_unknown_submit_pg.py tests/integration/test_orphan_quarantine_pg.py -m integration -q && uv run alembic check && uv run mypy src/ && uv run ruff check; then cd ../frontend && pnpm test && pnpm lint && pnpm build

Expected: PASS when PostgreSQL/integration services are available; otherwise integration commands report an explicit environment prerequisite rather than a false pass.

- [ ] **Step 5: Commit final runbook and architecture**

~~~bash
git add docs/runbooks/halt-2-projector-canary.md docs/runbooks/rollback-after-venue-write.md backend_py/ARCHITECTURE.md backend_py/tests/integration/test_halt2_replay_cutover.py
git commit -m "docs: publish halt two canary and rollback runbooks"
~~~

## Definition of Done

- Halt 2 has a read-only preflight, explicit persistent halt, empty-projection replay/hash and PENDING-to-UNKNOWN conversion.
- Fault injection proves one attempt/no retry across all ambiguous venue outcomes and process-crash boundaries.
- Canary is one account/one symbol/one minimal command, requires durable outcome plus two full-account reconciles, and cannot auto-ramp.
- Runbooks define safe rollback before and after venue writes; no unsafe DB restore assumption remains.
- Deployment scripts reject legacy identity/default realm and missing evidence.
- Full backend/frontend tests, Alembic drift, mypy and ruff pass before any operator considers resume.

## Explicitly Deferred Follow-up Plans

This P0 batch deliberately stops at the two clean cutovers and does not silently implement the later ADR workstreams. Before external beta, create and approve separate detailed plans for continuous offsite WAL/PITR and monthly restore drills; desired/applied config versions, transactional outbox, worker lease and CredentialProvider/KMS rotation; database RLS and static public proof artifacts; generated OpenAPI, immutable image promotion and CI/CD provenance; and customer onboarding, billing, Bitfinex consent/legal and economic non-inferiority gates.
