# VM Deploy + Cutover Implementation Plan

> **For agentic workers:** Tasks 1-4 are buildable/automatable (config + verify). Tasks 5-8 are OPERATOR-DRIVEN real-money runbook steps — do NOT let a subagent execute them autonomously; they require human confirmation, the Bitfinex account, and watching live behavior. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Run the bfx-funding-bot canary on the self-managed Oracle VM (Tokyo, Docker) via docker-compose (migrate one-shot + bot + autoheal), and cut over from Koyeb single-writer-safely (stop-old-then-start-new), with paper→shadow validation and a rollback runbook.

**Architecture:** A separate compose project at `/opt/bfx/` on the VM builds the image locally from `backend_py/Dockerfile`, runs alembic as a one-shot `migrate` service (the daemon `depends_on` it), supervises the `bot` daemon with `restart: unless-stopped` + an `autoheal` sidecar (acts on `/healthz` unhealthy, which plain compose ignores). The existing homelab obs stack auto-collects the bot's logs/metrics; we add Grafana alerts. Per-phase env files keep canary-only vars ABSENT in paper/shadow.

**Tech Stack:** Docker Compose, the bfx `python:3.13-slim` image (arm64), willfarrell/autoheal, Tailscale, the homelab Loki/Prometheus/Grafana stack.

**PREREQUISITE:** Plan A (`2026-05-31-bfx-hardening-for-self-host.md`) MUST be merged to `main` and Koyeb redeployed-healthy before Task 5+ (the advisory lock is the structural cutover safety). VM facts: `ssh -i ~/.ssh/id_ed25519_will413028 ubuntu@150.230.105.94`, Docker 29.5 + Compose v5.1, Tailscale up, obs stack running. Live Koyeb canary env (authoritative): cap=570, executor=bitfinex_live, phase=canary, realm=prod, cells=canary, scaling.min=1.

---

### Task 1: `docker-compose.bot.yml`

**Files:**
- Create: `docker-compose.bot.yml` (repo root, build context `backend_py`)
- Modify: `.gitignore` (ignore assembled/secret env)

- [ ] **Step 1: Create `docker-compose.bot.yml`**

```yaml
name: bfx

services:
  migrate:
    image: bfx-bot:local
    command: ["sh", "-c", "alembic upgrade head"]
    working_dir: /app
    env_file: .env.runtime
    restart: "no"

  bot:
    build:
      context: backend_py
      dockerfile: Dockerfile
      args:
        GIT_SHA: ${GIT_SHA:-unknown}
    image: bfx-bot:local
    container_name: bfx-bot
    command: ["bfx-shadow"]
    env_file: .env.runtime
    restart: unless-stopped
    stop_grace_period: 30s
    labels:
      autoheal: "true"
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:8080/healthz >/dev/null 2>&1 || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 180s
    depends_on:
      migrate:
        condition: service_completed_successfully

  autoheal:
    image: willfarrell/autoheal:latest
    restart: unless-stopped
    environment:
      AUTOHEAL_CONTAINER_LABEL: autoheal
      AUTOHEAL_INTERVAL: "30"
      AUTOHEAL_START_PERIOD: "180"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
```
> The healthcheck retries:3 × interval:30s ≈ 90s of sustained 503 before `unhealthy` → autoheal restarts. start_period:180s + AUTOHEAL_START_PERIOD:180 prevent boot flapping. 8080 is NOT published (no host port) → never public; healthcheck hits localhost inside the container.

- [ ] **Step 2: Add to `.gitignore`**

```
.env.runtime
bot.env
```

- [ ] **Step 3: Validate compose locally** (no build, just parse)

Run: `docker compose -f docker-compose.bot.yml config -q && echo "compose OK"`
Expected: `compose OK` (it will warn that `.env.runtime` is missing for env_file — create an empty one first: `touch .env.runtime` — that's fine, it's gitignored).

- [ ] **Step 4: Commit**

```bash
cd ~/second-brain/projects/startup/bfx-funding-bot
git add docker-compose.bot.yml .gitignore
git commit -m "🔧 Chore: add VM docker-compose (migrate one-shot + bot + autoheal)"
```

---

### Task 2: Per-phase env files

**Files:**
- Create: `deploy/vm/paper.env`, `deploy/vm/shadow.env`, `deploy/vm/canary.env`
- Create: `deploy/vm/bot.env.example` (secrets template — real `bot.env` lives only on the VM, gitignored)

- [ ] **Step 1: `deploy/vm/paper.env`** (ci realm, NO executor/keys/cap — all canary-only vars ABSENT)

```bash
BFX_PHASE=paper
BFX_DEPLOYMENT_ENV=ci
BFX_RUN_DURATION_HOURS=1
BFX_HEALTHZ_PORT=8080
```

- [ ] **Step 2: `deploy/vm/shadow.env`** (shadow realm, simulated, runs forever — canary-only vars ABSENT)

```bash
BFX_PHASE=shadow
BFX_DEPLOYMENT_ENV=shadow
BFX_HEALTHZ_PORT=8080
```

- [ ] **Step 3: `deploy/vm/canary.env`** (real money — the exact live Koyeb set; `BFX_CELLS_YAML` MUST be set)

```bash
BFX_PHASE=canary
BFX_DEPLOYMENT_ENV=prod
BFX_EXECUTOR=bitfinex_live
BFX_WS_CLIENT_ENABLED=true
BFX_ALLOCATION_CAP_USDT=570
BFX_CELLS_YAML=/app/configs/cells.canary.yaml
BFX_SAFETY_CONFIG=/app/configs/safety.canary.yaml
BFX_HEALTHZ_PORT=8080
```

- [ ] **Step 4: `deploy/vm/bot.env.example`** (secrets template — the REAL `bot.env` is created on the VM, chmod 600, gitignored)

```bash
# Copy to ~/bfx/bot.env on the VM, chmod 600, fill real values. Never commit.
DATABASE_URL=postgresql://USER:PASS@ep-...-pooler.ap-southeast-1.aws.neon.tech/DB   # raw Neon URL ok (code rewrites scheme/strips -pooler)
BFX_API_KEY=
BFX_API_SECRET=
BFX_ADMIN_TOKEN=
BFX_ACCOUNT_ID=default
BFX_SERVICE_VERSION=
```

- [ ] **Step 5: Commit**

```bash
git add deploy/vm/paper.env deploy/vm/shadow.env deploy/vm/canary.env deploy/vm/bot.env.example
git commit -m "🔧 Chore: per-phase VM env files (canary-only vars absent in paper/shadow)"
```

---

### Task 3: `scripts/deploy-vm.sh`

**Files:**
- Create: `scripts/deploy-vm.sh`

- [ ] **Step 1: Create `scripts/deploy-vm.sh`**

```bash
#!/bin/bash
# Deploy the bfx canary stack on the Oracle VM. Run ON THE VM from the repo root.
# Usage: ./scripts/deploy-vm.sh <paper|shadow|canary>
set -euo pipefail

PHASE="${1:-}"
case "$PHASE" in paper|shadow|canary) ;; *) echo "usage: $0 <paper|shadow|canary>"; exit 1 ;; esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
SECRETS="$HOME/bfx/bot.env"
PHASE_ENV="deploy/vm/${PHASE}.env"

[ -f "$SECRETS" ]   || { echo "ERROR: missing secrets $SECRETS (chmod 600)"; exit 1; }
[ -f "$PHASE_ENV" ] || { echo "ERROR: missing $PHASE_ENV"; exit 1; }

# Assemble the single env_file the compose references: secrets + phase config.
cat "$SECRETS" "$PHASE_ENV" > .env.runtime
chmod 600 .env.runtime

# Preflight: required vars present.
need_common="DATABASE_URL BFX_PHASE BFX_DEPLOYMENT_ENV"
need_canary="BFX_EXECUTOR BFX_WS_CLIENT_ENABLED BFX_API_KEY BFX_API_SECRET BFX_ALLOCATION_CAP_USDT BFX_CELLS_YAML BFX_SAFETY_CONFIG"
req="$need_common"; [ "$PHASE" = canary ] && req="$req $need_canary"
for v in $req; do
  grep -q "^$v=." .env.runtime || { echo "ERROR: required var $v missing/empty for phase $PHASE"; exit 1; }
done

# Real-money gate.
if [ "$PHASE" = canary ] && [ "${BFX_CANARY_CONFIRM:-}" != yes ]; then
  read -r -p "CANARY = REAL MONEY (cap 570). Type 'yes' to proceed: " ans
  [ "$ans" = yes ] || { echo "aborted"; exit 1; }
fi

git pull --ff-only origin main
export GIT_SHA="$(git rev-parse --short HEAD)"
docker compose -f docker-compose.bot.yml build --build-arg GIT_SHA="$GIT_SHA"
docker compose -f docker-compose.bot.yml up -d --remove-orphans
echo "deployed phase=$PHASE sha=$GIT_SHA"
docker compose -f docker-compose.bot.yml ps
```

- [ ] **Step 2: Lint + commit**

Run: `chmod +x scripts/deploy-vm.sh && bash -n scripts/deploy-vm.sh && echo "syntax OK"`
```bash
git add scripts/deploy-vm.sh
git commit -m "🔧 Chore: deploy-vm.sh (assemble env + preflight + build + up, real-money gate)"
```

---

### Task 4: Grafana alerts for the bot (in the homelab obs repo)

**Files:** (in `~/second-brain/projects/personal/homelab/observability/`)
- Create: `grafana/provisioning/alerting/bfx-rules.yml`

- [ ] **Step 1: Add alert rules** keying on the obs stack's existing collection (cAdvisor sees the `bfx-bot` container; Alloy ships its logs to Loki). Create `grafana/provisioning/alerting/bfx-rules.yml`:

```yaml
apiVersion: 1
groups:
  - orgId: 1
    name: bfx
    folder: alerts
    interval: 1m
    rules:
      - uid: bfx-bot-down
        title: bfx-bot container down
        condition: C
        for: 2m
        data:
          - refId: A
            relativeTimeRange: {from: 300, to: 0}
            datasourceUid: prometheus
            model:
              expr: 'count(container_last_seen{name="bfx-bot"})'
              instant: true
              refId: A
          - refId: C
            datasourceUid: __expr__
            model: {type: threshold, expression: A, conditions: [{evaluator: {type: lt, params: [1]}}], refId: C}
        labels: {severity: critical}
        annotations: {summary: 'bfx-bot container is not running'}
      - uid: bfx-guard-trip
        title: bfx guard tripped
        condition: C
        for: 0m
        data:
          - refId: A
            relativeTimeRange: {from: 300, to: 0}
            datasourceUid: loki
            model:
              expr: 'sum(count_over_time({container="bfx-bot"} |= "deployment_skip" [5m]))'
              refId: A
          - refId: C
            datasourceUid: __expr__
            model: {type: threshold, expression: A, conditions: [{evaluator: {type: gt, params: [0]}}], refId: C}
        labels: {severity: warning}
        annotations: {summary: 'bfx-bot logged a guard skip (deployment_skip)'}
```
> Reuses the obs stack's `prometheus`/`loki` datasource uids + the existing Telegram contact point/policy from the obs setup. Adjust the Loki match string after Task 5 once real log lines are visible (the exact submit/guard log keys: `deployment_skip`, `order_submit` — confirm in Loki).

- [ ] **Step 2: Deploy obs + verify rules loaded**

Run:
```bash
cd ~/second-brain/projects/personal/homelab/observability && ./deploy.sh
ssh -i ~/.ssh/id_ed25519_will413028 ubuntu@150.230.105.94 \
  'P=$(grep "^GF_ADMIN_PASSWORD=" ~/observability/.env|cut -d= -f2-); docker exec grafana wget -qO- "http://admin:$P@localhost:3000/api/v1/provisioning/alert-rules" | grep -o "\"title\":\"bfx[^\"]*\""'
```
Expected: lists `bfx-bot container down` + `bfx guard tripped`.

- [ ] **Step 3: Commit (homelab repo)**

```bash
cd ~/second-brain/projects/personal/homelab
git add observability/grafana/provisioning/alerting/bfx-rules.yml
git commit -m "homelab/obs: bfx-bot down + guard-trip Grafana alerts"
```

---

### Task 5: [OPERATOR] Paper validation on the VM (ci realm, NO real money)

> Run after Plan A merged. Proves the whole VM pipeline with zero money risk.

- [ ] **Step 1: One-time VM setup** — clone repo + secrets

```bash
ssh -i ~/.ssh/id_ed25519_will413028 ubuntu@150.230.105.94
# on VM:
git clone https://github.com/Will413028/bfx-funding-bot.git ~/bfx-funding-bot   # or rsync
mkdir -p ~/bfx && cp ~/bfx-funding-bot/deploy/vm/bot.env.example ~/bfx/bot.env && chmod 600 ~/bfx/bot.env
# edit ~/bfx/bot.env: DATABASE_URL (raw Neon prod URL) — for paper, keys can stay blank (paper has no executor)
sudo systemctl enable docker
```

- [ ] **Step 2: Deploy paper**

```bash
cd ~/bfx-funding-bot && ./scripts/deploy-vm.sh paper
```

- [ ] **Step 3: Verify (must be able to fail)**

```bash
docker compose -f docker-compose.bot.yml ps           # migrate Exited(0), bot Up
docker exec bfx-bot wget -qO- http://localhost:8080/healthz   # 200 after start_period
# obs: confirm Loki has the bfx-bot container
ssh ... 'docker exec loki wget -qO- "http://localhost:3100/loki/api/v1/label/container/values" | grep bfx-bot'
```
Expected: migrate exits 0, daemon boots, `/healthz` ok, logs flow to Loki, container exits ~0 after ~1h (paper auto-exits). If the ARM build fails or migrate errors → STOP, fix before proceeding.

---

### Task 6: [OPERATOR] Shadow validation on the VM (parallel with Koyeb — SAFE, different realm)

> shadow writes the `shadow` realm; Koyeb canary writes `prod`. No collision, no shared writer.

- [ ] **Step 1: Deploy shadow** — `cd ~/bfx-funding-bot && ./scripts/deploy-vm.sh shadow`
- [ ] **Step 2: Let it run and verify** sustained liveness, no false `/healthz` 503 on quiet funding cells, db keepalive, autoheal NOT flapping (`docker events --filter event=restart`), clean Grafana panels. Run long enough to trust the daemon on the VM (hours+). This de-risks without touching prod.

---

### Task 7: [OPERATOR] Cutover (real money — single-writer-safe)

- [ ] **Step 1: Pre-live gate** — re-run the 6-item gate in `docs/deploy/koyeb-canary.md`: API key has Funding write/create+cancel scope; funded wallet ≥ 570; G2 calibration audit pass; main HEAD has canary config (incl. Plan A's advisory lock + 57 threshold). **Take a Neon PITR snapshot point now.** Verify migration `b7c1d2e3f4a5` (drops `position_state`) is safe against the live prod DB.
- [ ] **Step 2: Fill canary secrets** on the VM — edit `~/bfx/bot.env`: real `BFX_API_KEY`/`BFX_API_SECRET` (Funding write+cancel), `BFX_ADMIN_TOKEN`, `DATABASE_URL`=Neon prod. (Provide secrets out-of-band; never paste into chat.)
- [ ] **Step 3: STOP Koyeb to ZERO (the critical step)**

```bash
koyeb service update bfx-funding-bot/marketfeed --scale 0
# then on the Bitfinex account: confirm NO resting funding offers + NO in-flight submit
```
Expected: Koyeb service scaled to 0 instances; Bitfinex shows zero live offers. **Do not proceed until provably zero prod-realm writer.**

- [ ] **Step 4: Start the VM canary**

```bash
cd ~/bfx-funding-bot && ./scripts/deploy-vm.sh canary   # type 'yes' at the real-money prompt
```
Expected: migrate runs once against Neon prod, `bot` boots, the advisory lock is acquired (single-writer guaranteed), `boot_recovery` reclaims any PENDING intents. If the lock can't be acquired the bot exits 75 (means a writer still exists somewhere → investigate before retry).

---

### Task 8: [OPERATOR] Post-cutover verification + rollback readiness

- [ ] **Step 1: Watch first hours** — Loki/Grafana for the first live `order_submit`/`ORDER_FILL` (proves write scope; a venue-permission error → flip kill switch + fix key immediately); `AllocationCapGuard` caps exposure at 570; `realized_loss_24h`(57)/drawdown(15%) guards live; L3 smoke `POST http://localhost:8080/admin/smoke-test?level=L3` (Bearer `BFX_ADMIN_TOKEN`, via `docker exec` or Tailscale) → `{status:ok,level:L3}`; `/healthz` stays 200 on idle.
- [ ] **Step 2: Keep rollback at hand**
  - HALT: `BFX_KILL_SWITCH=true` into `~/bfx/bot.env` (append) + `./scripts/deploy-vm.sh canary` (recreate) **THEN manually cancel unfilled offers on Bitfinex** (kill switch blocks new offers only).
  - Retreat to shadow: `./scripts/deploy-vm.sh shadow` (shadow.env omits all canary-only vars).
  - Code bug: `git revert` + `./scripts/deploy-vm.sh canary` (rebuilds locally).
  - Full abort: `docker compose -f docker-compose.bot.yml down`, confirm no live offers, then if needed `koyeb service update bfx-funding-bot/marketfeed --scale 1` — **never both up at once**. DB rollback via Neon PITR, never `alembic downgrade`.
- [ ] **Step 3: Boot-survival check** — reboot the VM once (`sudo reboot`), confirm the stack returns (`docker compose -f docker-compose.bot.yml ps`) via `restart: unless-stopped` + `systemctl enable docker`. (Do this BEFORE leaving the canary unattended.)
- [ ] **Step 4: Update wiki** — `wiki/projects/homelab/index.md` Workloads: bfx-funding-bot → "live on VM"; note Koyeb scaled-to-0 rollback target.

---

## Self-Review

- **Spec §B coverage:** B1 compose → Task 1; B2 env files → Task 2; B3 deploy-vm.sh → Task 3; B4 secrets → Task 2/5; B5 obs alerts → Task 4; B6 boot survival → Task 5/8. **§C cutover** → Tasks 5-8 (paper→shadow→pre-live→stop-Koyeb-to-0→start-VM→verify→rollback).
- **Placeholder scan:** none; env files have real values (cap 570 etc); `bot.env` secrets are operator-supplied by design (Task 5/7).
- **Consistency:** `.env.runtime` assembled by deploy-vm.sh is the single `env_file` the compose references; `bfx-bot` container name consistent across compose + healthcheck + alerts + rollback; image `bfx-bot:local` shared by migrate+bot.
- **Operator-task boundary:** Tasks 5-8 are explicitly human-driven (real money, Bitfinex account, build-on-ARM, reboot) — not for autonomous subagent execution.
