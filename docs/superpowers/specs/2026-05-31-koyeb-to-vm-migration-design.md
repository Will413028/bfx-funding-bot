# Koyeb → Oracle VM Migration — Design

Move the bfx-funding-bot canary off Koyeb (Docker-from-GitHub, implicit 1-instance) onto a self-managed Oracle Cloud Always-Free ARM A1 VM (Tokyo, 4 OCPU / 24GB), preserving real-money safety. Grounded against the codebase via a 13-agent understanding workflow (2026-05-31); the adversarial findings below are load-bearing.

## Confirmed current state (live, authoritative)

Read off the live Koyeb deployment `bfx-funding-bot/marketfeed` (deployment `9bc58477`):

- `BFX_ALLOCATION_CAP_USDT=570`, `BFX_EXECUTOR=bitfinex_live`, `BFX_PHASE=canary`, `BFX_DEPLOYMENT_ENV=prod`, `BFX_CELLS_YAML=/app/configs/cells.canary.yaml`, `BFX_SAFETY_CONFIG=/app/configs/safety.canary.yaml`, `BFX_WS_CLIENT_ENABLED=true`, kill switch unset, **scaling.min=1**.
- App = long-running daemon, console-script `bfx-shadow` = `bfx_funding_bot.modules.marketfeed.daemon:main`; `/healthz` on `:8080`.
- Deploy is **manual** (`deploy-koyeb.sh`; auto-deploy-on-push is OFF).
- External deps: Neon Postgres (Singapore, ap-southeast-1) + Upstash Redis.

## Decisions

| # | Decision | Choice |
|---|---|---|
| 1 | Single-writer enforcement | **pg advisory lock** at boot (fail-fast) + **per-submit fail-closed re-check**; not lease+fencing (venue can't validate a fencing token, so it adds no real-money safety) |
| 2 | Allocation cap | **Preserve 570** for the migration; graduating the cap is a separate post-migration decision (never change two risk dimensions at once) |
| 3 | `realized_loss_24h` limiter | Re-derive to **~10% of cap = 57 USDT** (was 15, sized for a 150 cap); drawdown stays 15% |
| 4 | `alembic upgrade head` location | **One-shot `migrate` compose service** (bot `depends_on` completed) + advisory lock + `lock_timeout`/`statement_timeout` in `env.py`; never auto-downgrade |
| 5 | Restart-on-unhealthy | `restart: unless-stopped` + **`autoheal` sidecar with debounce** (compose alone ignores unhealthy-but-alive) |
| 6 | Region | **Keep Tokyo VM + Neon Singapore** (~60-80ms verified non-material; DB off the order-critical path) |
| 7 | Redis | **Drop for canary** (`REDIS_URL` has zero consumers in src/) |

## Critical risks (adversarially confirmed) the design must neutralise

1. **Concurrent live writers = catastrophe, zero code guard.** Funding submit has no cid/idempotency; internal cid uses a fresh uuid4 per cycle so PKs never collide cross-process; venue assigns distinct offer ids so the event_log dedup index never fires; no advisory lock / leader / lease anywhere; two reconcilers absolute-overwrite `position_state` and each top up past the cap. Koyeb's implicit-1-instance was the only historical guard — the VM removes it. → Decision 1 (advisory lock) + cutover stop-old-then-start-new.
2. **Auto-restart-on-unhealthy gap.** Plain compose `restart:` only acts on process EXIT, not on a deadlocked-but-503-serving daemon. Without autoheal this is strictly worse than Koyeb. → Decision 5.
3. **Migration crash-loop / destructive DDL.** Inline `alembic upgrade head && exec bfx-shadow` under `restart:unless-stopped` crash-loops the trader on a bad migration and re-runs every cycle; `b7c1d2e3f4a5` DROPs+recreates `position_state`. → Decision 4 + Neon PITR snapshot before head-advancing deploy.
4. **Wrong-config → unsafe multi-currency.** Unset `BFX_CELLS_YAML` silently loads the 2-currency `cells.yaml`; the reconciler sizes everything against cell[0]'s wallet. → pin `BFX_CELLS_YAML`/`BFX_SAFETY_CONFIG`/`BFX_PHASE` in the env_file.
5. **Mis-sized loss limiters at 570.** → Decision 3.
6. **Hand-edited env footguns.** `BFX_EXECUTOR` defaults to `paper` (omit → silently trades nothing); leftover canary-only vars on flip-back reintroduce `ExecutorConfigError`. → per-phase env files where paper/shadow OMIT all canary-only vars + deploy preflight.

## Brain-dump corrections (baked into the design)

- VM is **Tokyo**, not Singapore — kept (latency non-material); fix stale "RTT<5ms co-located" notes.
- **Redis is dead plumbing** for the daemon — dropped.
- `DATABASE_URL` — code auto-rewrites scheme/strips `-pooler`; paste raw Neon URL.
- Cap conflict (150/450/570) — **570 is authoritative** (live service).
- Koyeb **autodeploy is OFF** — VM needs only a manual `git pull && build && up`, no webhook.
- Kill switch blocks NEW offers only — halt runbook MUST also manually cancel resting offers.

---

## Sub-project decomposition (strict ordering)

| Stage | Content | Where | When |
|---|---|---|---|
| **A. bfx hardening PR** | advisory lock + migrate split + loss-limiter re-derive + stale-doc fixes | bfx repo (TDD + review, merge main) | First — harmless to Koyeb (1 instance still acquires) |
| **B. VM deploy infra** | compose + 3-phase env + deploy script + autoheal + secrets + obs alerts | bfx repo (beside `deploy-koyeb.sh`) | After A merged |
| **C. Cutover** | paper→shadow→canary, stop Koyeb-to-0, rollback runbook | runbook | After B ready |

A's advisory lock is the structural safety foundation for the whole cutover and ships safely while Koyeb still runs.

## §A — bfx hardening PR

**A1 Single-writer advisory lock**
- Dedicated asyncpg connection (NOT from the pool — pool recycling would release the lock). At daemon boot, BEFORE `boot_recovery.run()`, acquire `pg_try_advisory_lock(hash(account_id, deployment_environment))`. Not acquired → log fatal + exit non-zero.
- Liveness task periodically `SELECT 1` on that connection, maintaining a `writer_lock_held` flag; connection drop = lock lost → trigger the daemon's fatal/halt path.
- Before every real-money submit (live executor submit path), read `writer_lock_held`; if false, **fail-closed (halt, do not submit)**. This bounds the duplicate-submit window under lock loss to ~zero — the only thing that actually protects the venue.
- Tests: second process fails to acquire → exits; lock-connection drop → submit refused.

**A2 Migrate split (no Dockerfile change — keeps Koyeb working)**
- Dockerfile default CMD stays `sh -c 'alembic upgrade head && exec bfx-shadow'` (Koyeb unaffected).
- VM splits via compose `command:` overrides: `migrate` service → `alembic upgrade head`; `bot` service → `bfx-shadow`.
- In alembic `env.py`, wrap migrations in a pg advisory lock + set `lock_timeout`/`statement_timeout` (benefits both Koyeb and VM: serialises concurrent starts, fails fast on stuck DDL).
- Never auto-run `alembic downgrade` (lossy; rollback via Neon PITR).

**A3 Loss-limiter re-derivation** — `safety.canary.yaml`: `realized_loss_24h` 15 → **57 USDT** (10% of 570); `drawdown_from_peak` stays 15%.

**A4 Stale-doc fixes** — `deploy-koyeb.sh:52` + `koyeb-paper.md:81` region notes → Tokyo reality; `koyeb-canary.md` cap 150 → 570.

## §B — VM deploy infra (in bfx repo, beside `deploy-koyeb.sh`)

**B1 `docker-compose.bot.yml`** — separate compose project from the obs stack, at `/opt/bfx/`:
- `migrate` (one-shot): `restart: "no"`, `command: alembic upgrade head`; bot `depends_on: {migrate: {condition: service_completed_successfully}}`.
- `bot`: locally built image, `command: bfx-shadow`, `restart: unless-stopped`, `container_name: bfx-bot`, `stop_grace_period: 30s`, `env_file: bot.env`, healthcheck `wget -qO- http://localhost:8080/healthz` (slim has no curl), `start_period: 180s`/`interval: 30s`/`retries: 3`. **8080 NOT published to 0.0.0.0** (carries the token-gated `/admin/smoke-test`); healthcheck hits localhost inside the container.
- `autoheal` (`willfarrell/autoheal`, mounts docker.sock): restarts `bot` when healthcheck is `unhealthy`, with a debounce (~3-5 consecutive 503s) so a WS blip / Neon hiccup doesn't restart a live trader.

**B2 Three-phase env files** — `paper.env` / `shadow.env` / `canary.env`. Canary-only vars are ABSENT (not blank) in paper/shadow (replicates Koyeb's load-bearing `!VAR` deletion). `canary.env` = the exact 8 canary vars above with `BFX_ALLOCATION_CAP_USDT=570` and `BFX_CELLS_YAML` set (unset → unsafe 2-currency fallback).

**B3 `scripts/deploy-vm.sh <phase>`** — `git pull origin main` → `docker compose -f docker-compose.bot.yml build --build-arg GIT_SHA=$(git rev-parse --short HEAD)` → `up -d`. Per-phase required-vars preflight; canary requires the `yes` real-money confirmation. Manual only.

**B4 Secrets** — `/opt/bfx/bot.env`, root-owned `chmod 600`, gitignored, compose `env_file:`. Carries only `DATABASE_URL` (raw Neon URL OK), `BFX_API_KEY`, `BFX_API_SECRET`, `BFX_ADMIN_TOKEN`. No Redis, no SaaS-layer JWT/AES/RESEND vars.

**B5 Observability integration (zero collection-side change)** — the existing cAdvisor + Alloy (docker.sock) already auto-collect the `bfx-bot`/`migrate` container metrics (CPU/mem/up/restart) and stdout logs into Prometheus/Loki. Only ADD Grafana alerts: bfx-bot container down/restart (cAdvisor), first live `order_submit` (log), guard trips / `/healthz` 503 (log) → Telegram.

**B6 Boot survival** — `systemctl enable docker` + `restart: unless-stopped`; verify the stack returns after an OCI reboot.

## §C — Cutover sequence (single-writer-safe)

Invariant: **exactly one live writer on the prod realm at any instant.**

1. **Pre-flight** — A merged; Koyeb redeployed-with-lock still HEALTHY; verify migration `b7c1d2e3f4a5` (drops `position_state`) safe against live prod DB; take a Neon PITR snapshot point; canary configs (incl. A3's 57) on main HEAD.
2. **Paper on VM (ci realm, no money)** — `deploy-vm.sh paper`. Verify: ARM build, migrate exits 0, daemon boots, `/healthz` 200, Alloy ships logs, autoheal healthy, container exits 0 after ~1h.
3. **Shadow on VM, parallel with Koyeb canary (safe — different realm)** — `deploy-vm.sh shadow` writes the `shadow` realm; Koyeb canary writes `prod`. No collision. Confirm sustained liveness, no false 503 on quiet cells, db keepalive, no autoheal flapping, clean Grafana.
4. **Pre-live gate (canary, real money)** — re-run the 6-item gate from `koyeb-canary.md` (API key Funding write/create+cancel scope — only proven by the venue permission page + the first live offer; wallet ≥ cap; G2 audit; main HEAD has canary config). Verify `canary.env` exact, `BFX_CELLS_YAML` set.
5. **Stop Koyeb to ZERO (most critical step)** — scale the Koyeb service to 0 instances (not pause, not stop-autodeploy). Confirm on the Bitfinex account: no resting offers, no in-flight submit. Provably zero prod-realm writer.
6. **Start VM canary** — `deploy-vm.sh canary` (type the real-money confirmation). Advisory lock guarantees single-writer; `boot_recovery` reclaims PENDING intents (designed for exactly this single-successor handoff); migrate runs once against the same Neon prod DB; daemon boots.
7. **Post-cutover verification (first hours)** — watch Loki/Grafana for the first live `order_submit`/`ORDER_FILL` (proves write scope; a venue permission error → flip kill switch + fix key immediately); AllocationCapGuard caps exposure at 570; `realized_loss_24h`/drawdown guards live; L3 smoke `POST /admin/smoke-test?level=L3` (Tailscale + Bearer) → `{status:ok,level:L3}`; `/healthz` stays 200 on idle (no 2026-05-26 idle-restart recurrence).
8. **Kill switch / rollback** — (a) HALT: `BFX_KILL_SWITCH=true` in bot.env + `up -d`, THEN manually cancel unfilled offers on Bitfinex (kill switch blocks new offers only); (b) retreat to shadow: swap to `shadow.env` (omits all canary-only vars) + recreate; (c) code bug: `git revert` + rebuild + up; (d) full abort: VM bot to 0, confirm no live offers, then if needed re-scale Koyeb canary to 1 — never both up at once. DB rollback via Neon PITR, never `alembic downgrade`.

## Out of scope (downstream)

- Graduating the canary cap (raise/remove 570) — separate decision after the VM canary proves stable.
- App-level Prometheus `/metrics` instrumentation (business metrics) — infra metrics only for now.
- Per-currency native allocation (the 2-currency `cells.yaml` path) — unshipped Phase-2 feature.
- Decommissioning Koyeb — keep scaled-to-0 as a warm rollback target until the VM canary is proven, then retire.
