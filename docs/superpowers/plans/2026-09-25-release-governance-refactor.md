# Release governance refactor — implementation plan

Branch: `refactor/release-governance`. Decisions: second-brain ADRs
`2026-09-25-automated-probation-replaces-release-ceremony` (governance) and
`2026-09-25-ci-registry-digest-deploy` (supply chain), both accepted 2026-09-25.
Production stays halted (halt 11) until the cutover in §4 completes.

## 1. Target behaviour

- **Supply chain**: CI builds arm64 images from a green `main` commit and pushes
  them to GHCR (done: `.github/workflows/release.yml`). The VM deploys by digest
  only, never builds, and rolls back to the previous digest on a failed start.
- **Change class**: computed on the VM from `git diff --name-only deployed..target`
  against `deploy/change-class.yaml` taken from the *target* commit. Default is
  `material`; only listed paths are `standard`. A class can only be raised.
- **Trading state** (durable, append-only, independent of releases):
  `ACTIVE`, `REDUCING` (cancels only; no new offer and no re-post), `HALTED`
  (writes the state, then venue funding cancel-all per enabled currency,
  including orphans and symbols with an unresolved UNKNOWN; needs only the
  writer lock). Cause: `operator | kill_switch | auto | material_deploy`.
- **Release flow**: standard → the previous trading state is kept. Material →
  `REDUCING` until the operator approves once (TOTP) → `ACTIVE` in probation:
  exposure ≤ 25% of the normal cell limit (at least one venue-minimum offer),
  lifted automatically after 24 h with ≥3 acknowledged submits and no automatic
  HALTED in between. Resume after an automatic HALTED also enters probation.
- **Automatic HALTED**: UNKNOWN submit, orphan quarantine, unclassifiable
  commitment, offer amount mismatch or venue lent amount above the internal
  ledger, loss limiter breach, writer lock loss. Realized decreasing because a
  loan ended (reconcile catching a missed WS event) is expected and never halts.
- **Always-on pre-trade limits**: CapitalPolicy (kept) plus absolute
  `max_offer_amount`, absolute rate floor, period bounds, max open offers per
  symbol, and a submit+cancel token bucket.
- **Alerts**: in-process Telegram sink (non-blocking) for trading-state changes,
  automatic protections, UNKNOWN, orphan, divergence that halts, backup failure,
  restore-test failure, deploy/rollback. Grafana stays as the second path.
- **DR**: backups and WAL archiving unchanged; freshness alert; a monthly automated
  isolated restore verified by comparing the restored `event_prefix_hashes` with
  production's at the same sequence (no writer quiescence), plus an extra run after
  a PostgreSQL upgrade, a pgBackRest/R2 config or credential change, and before an
  irreversible migration. Heartbeat on success, Telegram on failure. Nothing about
  DR gates a release or resume. A migration is always preceded by a successful backup
  (Will, 2026-09-25).

## 2. Tasks

Each task lands with regression tests; money-path tasks need a mutation check.
Commands: `cd backend && uv run pytest -m "not integration"`, the affected
integration tests (Docker), `uv run mypy src/`, `uv run ruff check`,
`uv run alembic check`; frontend `pnpm vitest run`, `pnpm tsc --noEmit`,
`pnpm biome check src/`.

| # | Task | Depends | Acceptance |
|---|---|---|---|
| T1 | Trading state schema + repository (append-only, cause, probation fields); migrate the current halt row to `HALTED/operator` | — | state survives restart; illegal transitions rejected by DB trigger and code |
| T2 | Guards by state: `REDUCING`/`HALTED` block submits; cancels no longer call `check_normal` or the halt check; flip `test_capital_command_boundary.py:220,405` | T1 | cancel allowed while halted; submit blocked in both states |
| T3 | Kill path: `HALTED` → venue `POST /v2/auth/w/funding/offer/cancel/all` per enabled currency, bypassing provenance/uncertainty guards; UNKNOWN resolution accepts "cancelled by us" | T1, T2 | integration test with a fake venue: orphan and UNKNOWN-symbol offers cancelled |
| T4 | Automatic protections write `HALTED/auto` (triggers in §1); divergence classified so loan-end catch-ups never halt | T3 | one test per trigger; replay of the five production divergences (2026-08-27..09-24) does not halt |
| T5 | Change class + probation: rules file, VM-side classifier, probation multiplier in `evaluate_capital`, bake evaluator, approval + resume endpoints (webapi, MFA via BFF) | T1, T4 | material deploy sits in REDUCING; approval → probation → lifted after criteria; auto HALTED resets |
| T6 | Remove the ceremony: release worker/session/tables/API, canary permits, epoch renewal, `release_identity` launch receipts, `halt2_cutover`, canary preflight scripts, daemon Canary* wiring; migration archives then drops release/permit tables (real-money history kept); boot-time schema head check and audit identity move to the image digest | T5 | Phase 2 deletion list from the Phase 0 inventory is gone; unit + integration green |
| T7 | Frontend: trading state panel, approve/resume (TOTP), kill switch; remove release panel | T5, T6 | vitest + manual browser check at cutover |
| T8 | Telegram alert sink + event wiring | T1 | sink failure never blocks trading; each event in §1 has a test |
| T9 | Pre-trade limits in §1 | — | one guard test each, fail-closed when config is missing |
| T10 | VM tooling: `deploy/vm/docker-compose.app.yml` (same hardening as today: read-only rootfs, UID 1000, cap-drop ALL, no-new-privileges, frontend on 127.0.0.1:3001), `bfx-deploy` (discover `main`, pull by digest, backup+migrate, recreate, health, rollback, ledger row), systemd timer; weekly restore-test timer + heartbeat; backup freshness alert | T5, T8 | dry-run on the VM; rollback exercised once on purpose |
| T11 | D4' outbox (branch `worktree-agent-ae8a9e90a4b11dd54`, `4e99d6f`) merged in | T6 | its tests green on this branch |
| T12 | Runbooks: rewrite deploy/operate/DR; delete immutable-release, halt-2, release-0 and superseded sections; delete VM-only scripts under `/opt/bfx/tooling` after cutover | T10 | no runbook references removed commands |

Parallel lanes (disjoint files): T8 and T9 alongside T1–T5; T10 after T5's
interfaces are fixed.

## 3. Human prerequisites (Will)

- Bitfinex: confirm the API key has no withdrawal permission, set an IP
  whitelist for the VM, confirm account-level auto-renew is off.
- Telegram: create a bot, store token and chat id in both `/opt/bfx/runtime/bot.env`
  (in-process sink) and `/opt/bfx/runtime/notify.env` (host tools) on the VM, reply
  `done` (never paste them). Changing the token later means changing both files.
- GHCR: a read-only token for the VM in `/opt/bfx/runtime/ghcr.env`, stored the same way.

## 4. Cutover

Step-by-step commands: `docs/runbooks/cutover-release-governance.md` (T12).

1. Merge the branch after review; CI publishes the first images.
2. On the VM: install compose file, `bfx-deploy`, timers; add secrets (§3).
3. Stop the old app containers (keep them renamed as recovery copies).
4. `bfx-deploy` once by hand: backup → migrations (outbox, trading state,
   archive+drop release tables) → start → health.
5. Verify: state `HALTED/operator` carried over, alerts arrive, kill switch
   cancels a test-free account (no open offers expected), UI shows state.
6. ~~Revoke the five manual webapi grants~~ — superseded: migration `6f2b8d0e4a17`
   versions the web API's read grants (`9136d2b`).
7. Will resumes with TOTP → probation (25%, 24 h) → normal.

**Rollback** before step 4 migrations: restart the renamed old containers.
After migrations: the old code cannot run on the new schema; roll forward, or
restore the pre-migration backup to an isolated database first, compare, then
decide (never restore over post-write venue reality; see
`docs/runbooks/rollback-after-venue-write.md`).

## 5. Progress

- [x] Release workflow (`1a56aee`)
- [x] T1 trading state `afa1de6`; T2 guards by state `8ce9a0d`; T3 kill path `7dd847a`
- [x] T10 VM tooling `5030e5d`, merged `97c7612`, migration chain `b0b6067`
- [x] T10b monthly restore test with prefix-hash verification + change-triggered runs `d75b288`
- [x] T4 automatic protections through the kill path; no trading_state row means HALTED `1b43255` (+ identity conflicts `0c94084`)
- [x] T5 change class + approval + probation `998cc73` (+ `2711e3a`)
- [x] T8 Telegram alert sink `da63b6f`
- [x] T9 always-on pre-trade limits `758590e`, merged `0c2a99f`; single migration head `ebc943f`
- [x] T11 D4' outbox merged `ea10d60`
- [x] T6 ceremony removed, release tables archived `8422d13` (merged into the deploy tooling `51e3773`)
- [x] T7 frontend trading state panel `2c639fa`
- [x] Resume only through TOTP + versioned web API read grants `9136d2b`; boot refusal cancels venue offers `cfe0f87`; kill-path flaky test `a3e5071`
- [x] T12 runbooks: `deploy.md`, `operations.md`, `cutover-release-governance.md`; `immutable-release.md` and `halt-2-projector-canary.md` deleted; release-0, rollback-after-venue-write, offsite-dr, AGENTS/README rewritten (this commit)
- [ ] Cutover (§4) — Will's prerequisites (§3), then the cutover runbook; afterwards one rollback drill and the VM cleanup list in the cutover runbook §7
