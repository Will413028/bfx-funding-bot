# DR RTO Measurement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce trustworthy stage-level cold-restore measurements and an evidence-backed optimization proposal without changing acceptance thresholds.

**Architecture:** Instrument existing restore lifecycle with monotonic stage durations while preserving its global deadline, isolation and cleanup. Measure independently on generated disposable resources; optimization implementation follows only after the measured proposal is approved.

**Tech Stack:** Python, pytest, Docker, PostgreSQL 18, pgBackRest/R2.

**Spec:** `docs/superpowers/specs/2026-09-10-projection-audit-cutover-design.md`, section 5.

## Global Constraints

- 維持 Halt 2 **RPO <=300 秒、RTO <=3600 秒**；此為與既有 operational target
  對齊的已核准門檻。
- 不得把成本移到計時前或使用預先還原的 volume 冒充冷恢復。
- 變更 OCI 容量／性能計費、備份範圍、retention 或 RTO 定義需另行核准。
- 不新增自動 retention／清除政策；不刪除已封存歷史來縮短 restore。
- 若實測仍無法達成，明確回報阻擋，不承諾恢復日期、不放寬門檻。
- No production writes, credential output, automatic tailnet switch, paid resource updates or trading resume.

## Dependencies

Use an isolated branch containing the reviewed historical replay fix. Serialize modifications to
`restore_drill.py` and its tests with Task 4 of `2026-09-10-projection-audit-cutover.md`.
Run representative post-cutover measurements only after archive-aware verification is integrated.
Earlier timings remain diagnostic, not final release evidence. Execution must read both plans.

### Task 1: Bounded stage timing without changing acceptance semantics

**Files:** Create `deploy/vm/pgbackrest/restore_timing.py`,
`backend_py/tests/scripts/test_restore_timing.py`;
modify `deploy/vm/pgbackrest/restore_drill.py`,
`backend_py/tests/scripts/test_offsite_dr_restore.py` and `docs/runbooks/offsite-dr.md`.

**Interfaces:** `StageTiming(clock: Callable[[], float])`,
`begin(name: str) -> None`, `end(name: str) -> None`, `render() -> dict[str, float]`.
Allowlist stages `resource_setup`, `physical_and_wal_recovery`, `isolation_bootstrap`,
`verification`, `cleanup`. No free-form labels, raw commands, DB URLs, errors or payloads.
This is diagnostic evidence separate from the existing accepted restore receipt.

- [ ] Write RED tests using a finite fake clock:

  ```python
  def test_measures_elapsed_without_using_wall_clock():
      samples = iter([10.0, 12.5])
      timing = StageTiming(clock=lambda: next(samples))
      timing.begin("verification")
      timing.end("verification")
      assert timing.render() == {"verification": 2.5}
  ```

  Also reject unknown/duplicate/unclosed stages, non-finite clock values, backwards time and
  overlapping stages. Do not emit a complete-success diagnostic for unfinished stages.
- [ ] Run `uv run pytest tests/scripts/test_restore_timing.py -q` in backend_py; retain RED.
- [ ] Implement start/end around existing lifecycle boundaries; do not move the original
  RTO start/end or reset deadlines. Keep recovery grouped in runner diagnostics; split physical
  restore vs WAL only using pgBackRest's actual stage log evidence, never subtract guessed delays.
- [ ] Add failure-injection tests: failed restore still executes cleanup within its independent
  30-second bound; failure to persist diagnostics cannot skip cleanup or promote failed evidence.
  Verify accepted `rto_seconds` is unchanged for identical existing fake-clock traces.
- [ ] Run `uv run pytest tests/scripts/test_restore_timing.py tests/scripts/test_offsite_dr_restore.py -q`;
  document diagnostic meaning, commit and request independent review. No optimization settings yet.

### Task 2: Operator benchmark and optimization decision package

**Files:** Private operator evidence only; no production configuration or Terraform edits.

**Interfaces:** consumes reviewed image/config/runner and matching backup/baseline/manifests;
produces raw protected measurements plus a sanitized recommendation distinguishing measured
evidence, inference, proposed changes and operator approvals.

- [ ] Confirm correct authorized network and target VM with read-only checks. If current profile
  is company, do not switch automatically. Inventory CPU/RAM, exact attached volume/performance,
  Docker image/config and existing background workload without exposing credentials.
- [ ] Use generated empty volumes and unique reports; restore the same selected backup with
  the same full verifier and archive inventory. Record image IDs, target, observed times,
  stage durations and cleanup. An unsuccessful replay can characterize elapsed stages but
  cannot yield accepted RTO or be used as evidence of restored trading.
- [ ] Collect CPU utilization, block I/O and network counters during the run using available
  read-only host tools. Preserve units and sampling periods. Separate download/decryption/
  decompression/write hypotheses; one utilization snapshot does not establish a bottleneck.
- [ ] Prepare a single-variable comparison only for a supported, reversible setting on the
  isolated restore. Read current installed tool help/primary vendor docs before selecting it.
  No additional uncontrolled tuning sweep; current process-max/archive-mode comparisons already
  failed the former sub-minute benchmark. If no supported safe candidate is justified, report that fact.
- [ ] Compare at least three baseline and three candidate runs only after the candidate is
  approved, with no overlapping restores and identical verified data scope. Report each duration
  plus min/median/max, cold volume policy and remote/local cache caveats; never cherry-pick best run.
- [ ] Optimization proposal must state expected bottleneck, measured impact, correctness
  regressions to test, rollback and possible cost. Hardware/payment, restore scope or threshold
  changes stop for separate approval; no automatic Terraform apply.
- [ ] Archive+active parity, fresh RPO<=300 and full measured RTO<=3600 are all required for later
  release. A successful benchmark does not authorize production cutover, canary or resume.

## Self-review coverage

Spec section 5 timing/cleanup/integrity → Task 1. Resource attribution and single-variable
optimization/cost boundaries → Task 2. No specific optimization is preselected without evidence;
this plan is complete when trustworthy measurement and an actionable decision package exist,
not when trading is restored. The overall trading goal remains incomplete until its separate gates pass.
