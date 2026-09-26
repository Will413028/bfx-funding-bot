# Automatic resume of an automatic halt — implementation plan

Branch: `feat/auto-resume`. Decision: second-brain ADR
`2026-09-26-auto-halt-resumes-when-condition-clears` (replaces the "only an
operator ends HALTED" part of lending envelope D4). Parameters agreed by Will
2026-09-26: 15 min minimum halt, 3 consecutive clean accepted snapshots,
at most 2 automatic resumes per rolling 24 h.

## 1. Target behaviour

- `HALTED/auto` → `ACTIVE/auto` (actor `auto-resume`) when the halt is at least
  15 min old and the last 3 accepted snapshots after it tripped nothing. Any
  trip, including a persisting condition while halted, empties the count.
- The count lives in `AutomaticProtection` (memory; a restart starts it again).
  Reconcile's `_protect` reports `observe_clean(event_seq)` for an accepted
  snapshot with no trip and no fence refusal; the protection's supervised task
  evaluates the resume, so a halt and a resume are never written concurrently.
- An operator HALTED over `HALTED/auto` is written (not a restatement) and never
  resumes automatically; an automatic halt still restates an operator one.
- PostgreSQL is the authority (migration `8e4b2f6a1c37`): `ACTIVE/auto` only
  from `HALTED/auto`, at least 15 min after it and with fewer than 2 `ACTIVE/auto`
  rows in the last 24 h, both on the database clock; every `cause = 'auto'` row
  is stamped with that clock, so the bot's own time cannot shorten either.
  Rejections carry SQLSTATE `BX001`/`BX002`/`BX003`, mapped to
  `IllegalTradingTransition`/`AutoResumeTooSoon`/`AutoResumeLimitReached`.
  Python mirrors the rules (`validate_transition`, `count_auto_resumes`) for the
  early error and for SQLite tests, which have no trigger; refused past the
  limit → `auto_resume_limit_reached` alert once per halt.
- Edge-detected triggers (ADR D5, Will 2026-09-26): `venue_lent_above_ledger`
  and `command_rate_exceeded` cannot re-observe while halted, so for them the
  resume means "did not recur"; the 2-per-day limit stops a recurring bug.
- Trips and clean observations share one inbox, handled in arrival order; a
  trip in a batch outranks a clean observation queued with it.

## 2. Tasks and acceptance

| # | Task | Acceptance |
|---|---|---|
| A1 | Trigger migration + Python rule mirror + `restates()` | parity test pins who may end an automatic halt (fresh, young, after two resumes, a day later, operator) on migrated PG |
| A2 | `AutomaticProtection.observe_clean` / `resume_if_cleared` / run-loop wakeup; `_protect` hook | unit: resume at 3 clean + 15 min, not at 2, trip restarts count, pre-halt clean ignored, operator halt kept, third halt stays + one alert; integration: never-trip replays report clean, a tripping snapshot does not; mutation on each guard |
| A3 | Alert `auto_resume_limit_reached` | alert test above |
| A4 | Docs: ARCHITECTURE §6, operations runbook §1/§4 | no remaining "never lifted automatically" |

## 3. Deploy

The release carries a migration, so bfx-deploy stops the bot → backup →
restore test → migrate → start. It needs the DR checkout fix of PR #20
(`d9cf002`, deployed 2026-09-26 as ledger #8), whose tooling is now installed.
The frontend already renders `ACTIVE · automatic protection`; no UI change.

## 4. Not live-verified

No level-3 trip is induced in production to watch a resume; the behaviour is
covered by the unit, integration and parity tests above.
