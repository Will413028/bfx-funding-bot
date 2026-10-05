#!/usr/bin/env python3
"""Switch the production capital authority from legacy to ledger, in one run Will starts.

Runs as root, by hand, once (docs/runbooks/ledger-switch.md); no unit or timer runs it
(tests/architecture/test_ledger_seed_dormant.py). It drives the halt end to end with the same
patterns as bfx-deploy (Runner abstraction, flock, Telegram notify through bfx_notify):

P-checks, any failure ends the run before anything is stopped:
  the running backend digest is the one given on the command line (the digest the soak passed
  on) and the deployments ledger's newest attempt deployed it; the soak's result file
  (`sim_soak_report.py --result-out`) says PASS for that digest, its last generation being that
  release's revision; `alembic current` equals
  `alembic heads` in a one-shot of that image; bfx-weekly-report.service is not running; no
  bfx-sim container runs; no runtime session (bfx_bot, bfx_webapi or a member, or any session
  on the bfx_sim database) exists other than the bot's and the web API's own containers; the
  seed's read-only `--check` exits 0 (refusals that only mean "a submit or query is in flight"
  are re-checked a few times).

Steps:
  1. stop bfx-deploy.timer, bfx-weekly-report.timer, bfx-restore-test.timer and
     bfx-pgbackrest-backup.timer (each restarted at the end only if it was active), then take
     bfx-deploy's flock (bounded wait): no deploy runs, and none can start until step 8;
  2. record the resting offers of the latest legacy snapshot (the halt's exposure: resting
     offers stay on the book, no kill, no cancel-all);
  3. stop bfx-webapi (no new operator request);
  4. wait (bounded) for one legacy accepted snapshot whose query started after step 3 (F5:
     every resolution precedes the final snapshot);
  5. stop bfx-bot;
  6. pgBackRest backup with the deployed release's backup.sh; the new label is recorded (the
     only backup `restore-halt-backup` may restore);
  7. the seed `--switch` in a one-shot of the same image as the owner (DSN file derived from
     /opt/bfx/runtime/migrate.env file to file, never printed, deleted afterwards): seed,
     capture-point closure verifier, `ledger` epoch, digest verification, one transaction;
     F7 (live credits still `recent_fill`) above 20 % of a symbol's capital notifies;
  8. release the flock, `bfx-deploy --recreate` (same digest; bot and web API boot ledger);
  9. wait (bounded) for a non-seed accepted ledger observation, the latest runtime basis
     without `unexplained_lending` on any symbol, trading_state ACTIVE and web API /ready 200;
 10. report-only, never a rollback: NAV continuity once (each symbol's total capital of the
     first runtime basis against the last legacy snapshot at halt start, `NAV_TOLERANCE`), and
     a transient systemd timer that runs `watch --run-id <id>` 24 h later, which notifies when
     no acknowledged submit and no reprice (a managed offer canceled) happened since the switch;
 11. restart the timers, notify success with the evidence path.

Failure branches (each notifies immediately):
  R1 before the seed committed (the database still says `legacy`): start bfx-bot and
     bfx-webapi on legacy, restart the timers. A refusal that means "stopped mid submit or
     mid query" (attempt_outcome_missing, legacy_query_pending) first lets legacy boot recovery
     finish (one accepted snapshot after the restart), then the whole run is retried once.
  R2 after the seed committed, before the first runtime ledger write (no non-seed ledger
     observation): bfx-bot stays stopped; the notification names the failure, the exposure and
     the two options: forward-fix (a fixed release, or --recreate after a config fix), or
     `restore-halt-backup --run-id <id>` (the approved deploy.md exception).
  R3 after the first runtime ledger write: forward-fix only; nothing is stopped (protection
     halts by itself on an exposure it cannot explain).
  The timers are restarted on every exit path after step 1.

`restore-halt-backup --run-id <id>` restores the run's halt backup in place, only while no
runtime ledger observation exists: timers stopped and deploy flock held, bot/web API/frontend
stopped, postgres stopped, `pgbackrest restore --delta --set=<label> --type=immediate`
through a one-shot of the postgres image with its volumes, postgres started, epoch `legacy`
and no seed rows verified, a fresh full backup taken at once, legacy started, timers restarted.

`preflight` runs the P-checks only. Every run writes /var/lib/bfx-ledger-switch/<run id>/
evidence.json: one entry per step (start and end ms, result, detail), the resting exposure,
the backup label, the seed's summary and F7, and the outcome; the seed's JSONL output sits
next to it.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import secrets
import shutil
import signal
import sys
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import ModuleType
from typing import IO, Any, Protocol
from uuid import UUID

_HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bfx_deploy = _load("_bfx_ops_deploy", _HERE / "bfx_deploy.py")
bfx_notify = bfx_deploy.bfx_notify

CommandError = bfx_deploy.CommandError
DeployError = bfx_deploy.DeployError
Runner = bfx_deploy.Runner

SEED_MODULE = "bfx_funding_bot.apps.ledger_seed"
TIMERS = (
    "bfx-deploy.timer",
    "bfx-weekly-report.timer",
    "bfx-restore-test.timer",
    "bfx-pgbackrest-backup.timer",
)
WEEKLY_SERVICE = "bfx-weekly-report.service"
RUNNING_STATES = frozenset({"active", "activating", "deactivating", "reloading"})
SIM_CONTAINER = "bfx-sim"
# Refusals that only mean legacy stopped mid submit (F6) or mid query: legacy boot recovery
# settles them; the run retries once after it.
TRANSIENT_REFUSALS = frozenset({"attempt_outcome_missing", "legacy_query_pending"})
F7_NOTIFY_SHARE = Decimal("0.20")
# NAV continuity across the halt, per symbol: |ledger total - legacy total| / legacy total.
# The halt legitimately moves a symbol's total (available + offered + credits) only through
# interest credited to the wallet while the bot was stopped (Bitfinex pays daily; at most
# ~0.1 %/day of what is lent at the rates the bot trades) and foreign offers placed or
# removed by hand; fills move amounts between offered and credits and expiries back to
# available, neither changes the total. 0.5 % covers a halt of a few days of interest; more is
# a deposit, a withdrawal or a counting difference and is worth a look (report only).
NAV_TOLERANCE = Decimal("0.005")
WATCH_DELAY = "24h"
SEED_INPUT_MOUNT = "/run/bfx-seed"
EXIT_OK, EXIT_PRECHECK, EXIT_R1, EXIT_R2, EXIT_R3, EXIT_USAGE = 0, 10, 11, 20, 30, 2
EXIT_RESTORE_REFUSED, EXIT_RESTORE_FAILED = 40, 41
PROBE = bfx_deploy.PROBE


def log(message: str) -> None:
    print(f"bfx-ledger-switch: {message}", file=sys.stderr, flush=True)


class Terminated(Exception):  # noqa: N818 - a signal, not an error
    """SIGTERM (systemd's TimeoutStartSec, `systemctl stop`): taken as a failure of the current
    step, so the failure branch, the timer restart and the notification still run."""


def _terminate(signum: int, frame: object) -> None:
    raise Terminated(f"signal_{signum}")


class SwitchError(Exception):
    """A bounded failure code; never carries secrets or raw command output."""

    def __init__(self, code: str, *, reason: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason  # the seed's refusal code, when the seed refused


def _as_switch_error(exc: BaseException) -> SwitchError:
    if isinstance(exc, SwitchError):
        return exc
    if isinstance(exc, DeployError):
        return SwitchError(exc.code)
    if isinstance(exc, Terminated):
        return SwitchError("terminated")
    return SwitchError(f"unexpected:{type(exc).__name__}")


# --------------------------------------------------------------------------- database


@dataclass(frozen=True, slots=True)
class SnapshotHead:
    event_seq: int
    query_started_at_ms: int
    blocked: bool
    resting: Mapping[str, Mapping[str, str]]  # symbol -> offered / foreign
    offers: int
    totals: Mapping[str, str] = field(default_factory=dict)  # available + offered + credits


@dataclass(frozen=True, slots=True)
class RuntimeBasis:
    accepted: bool
    unexplained: tuple[str, ...]


class SwitchDb(Protocol):
    def foreign_runtime_sessions(self, own_addresses: Sequence[str]) -> int: ...
    def latest_snapshot(self, account: str, environment: str) -> SnapshotHead | None: ...
    def epoch_authority(self) -> str: ...
    def observations(self, account: str, environment: str) -> tuple[int, int]: ...
    def latest_runtime_basis(self, account: str, environment: str) -> RuntimeBasis | None: ...
    def trading_state(self, account: str, environment: str) -> str | None: ...
    def runtime_totals(self, account: str, environment: str) -> dict[str, str]: ...
    def activity_since(self, account: str, environment: str, since_ms: int) -> tuple[int, int]: ...


_SESSIONS = """
WITH RECURSIVE writers(oid) AS (
  SELECT oid FROM pg_roles WHERE rolname IN ('bfx_bot', 'bfx_webapi')
  UNION
  SELECT m.member FROM pg_auth_members m JOIN writers w ON m.roleid = w.oid)
SELECT json_build_object('count', count(*)) FROM pg_stat_activity a
WHERE a.pid <> pg_backend_pid()
  AND (a.usesysid IN (SELECT oid FROM writers) OR a.datname = 'bfx_sim')
  AND (a.client_addr IS NULL
       OR NOT (host(a.client_addr) = ANY (string_to_array(NULLIF(:'own', ''), ','))));
"""
_SNAPSHOT = """
SELECT coalesce((SELECT json_build_object(
  'event_seq', s.event_seq, 'started_at_ms', q.started_at_ms,
  'blocked', s.authorization_blocked_reason IS NOT NULL,
  'symbols', s.classification -> 'symbols', 'offers', s.classification -> 'offers')
  FROM public.capital_snapshots s
  JOIN public.capital_snapshot_queries q ON q.id = s.query_id
  WHERE s.exchange_account_id = :'account'::uuid AND s.deployment_environment = :'env'
  ORDER BY s.event_seq DESC LIMIT 1), 'null'::json);
"""
_EPOCH = """
SELECT json_build_object('authority', (SELECT authority FROM public.capital_authority_epoch
  ORDER BY epoch_seq DESC LIMIT 1));
"""
_OBSERVATIONS = """
SELECT json_build_object(
  'seed', count(*) FILTER (WHERE o.origin = 'legacy_seed'),
  'runtime', count(*) FILTER (WHERE o.origin <> 'legacy_seed' AND b.accepted))
FROM public.ledger_observation o
LEFT JOIN public.accepted_capital_basis b ON b.observation_id = o.id
WHERE o.exchange_account_id = :'account'::uuid AND o.deployment_environment = :'env';
"""
_RUNTIME_BASIS = """
SELECT coalesce((SELECT json_build_object(
  'accepted', b.accepted,
  'unexplained', coalesce((SELECT json_agg(s.symbol ORDER BY s.symbol)
     FROM public.accepted_capital_basis_symbol s
     WHERE s.basis_id = b.id AND s.conservation = 'unexplained_lending'), '[]'::json))
  FROM public.accepted_capital_basis b
  JOIN public.ledger_observation o ON o.id = b.observation_id
  JOIN public.ledger_observation_query q ON q.query_id = o.query_id
  WHERE o.origin <> 'legacy_seed'
    AND b.exchange_account_id = :'account'::uuid AND b.deployment_environment = :'env'
  ORDER BY q.query_revision DESC LIMIT 1), 'null'::json);
"""
_RUNTIME_TOTALS = """
SELECT coalesce((SELECT json_object_agg(s.symbol, (s.available + s.offered + s.credits)::text)
  FROM public.accepted_capital_basis_symbol s WHERE s.basis_id = (
    SELECT b.id FROM public.accepted_capital_basis b
    JOIN public.ledger_observation o ON o.id = b.observation_id
    JOIN public.ledger_observation_query q ON q.query_id = o.query_id
    WHERE o.origin <> 'legacy_seed' AND b.accepted
      AND b.exchange_account_id = :'account'::uuid AND b.deployment_environment = :'env'
    ORDER BY q.query_revision DESC LIMIT 1)), '{}'::json);
"""
# Acknowledged submits the runtime made, and managed offers it canceled (a reprice cancels,
# then submits), both after the switch.
_ACTIVITY = """
SELECT json_build_object(
  'acked', (SELECT count(*) FROM public.submission_attempt_journal a
            JOIN public.transport_outcome_journal t ON t.attempt_id = a.attempt_id
            WHERE a.exchange_account_id = :'account'::uuid AND a.deployment_environment = :'env'
              AND a.seed_provenance IS NULL AND t.kind = 'ack'
              AND t.completed_at_ms > :'since'::bigint),
  'canceled', (SELECT count(DISTINCT h.venue_offer_id)
            FROM public.ledger_observation_offer_history h
            JOIN public.ledger_observation o ON o.id = h.observation_id
            WHERE o.exchange_account_id = :'account'::uuid AND o.deployment_environment = :'env'
              AND h.terminal_kind = 'canceled' AND h.occurred_at_ms > :'since'::bigint
              AND h.venue_offer_id IN (SELECT t.venue_offer_id
                                       FROM public.transport_outcome_journal t
                                       WHERE t.kind = 'ack')));
"""
_TRADING_STATE = """
SELECT json_build_object('state', (SELECT state FROM public.trading_state
  WHERE exchange_account_id = :'account'::uuid AND deployment_environment = :'env'
  ORDER BY id DESC LIMIT 1));
"""


class PsqlSwitchDb:
    """Reads through `docker exec <postgres> psql` as the owner role (as PsqlLedger does).

    Values travel as psql variables (`:'name'`); every query returns one JSON line.
    """

    def __init__(self, runner: Runner, *, container: str, db_user: str, db_name: str) -> None:
        self._runner = runner
        self._container = container
        self._db_user = db_user
        self._db_name = db_name

    def _json(self, sql: str, variables: Mapping[str, str] | None = None) -> Any:
        argv = ["docker", "exec", "-i", "--user", "postgres", self._container,
                "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                "-h", "/var/run/postgresql", "-U", self._db_user, "-d", self._db_name]
        for key, value in (variables or {}).items():
            argv += ["-v", f"{key}={value}"]
        result = self._runner(argv, input_text=sql, timeout=60)
        if result.returncode != 0:
            log(f"psql failed: {result.stderr.strip()[-300:]}")
            raise SwitchError(f"psql_exit_{result.returncode}")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        try:
            return json.loads(lines[-1])
        except (IndexError, ValueError):
            raise SwitchError("psql_output_unparsable") from None

    def foreign_runtime_sessions(self, own_addresses: Sequence[str]) -> int:
        return int(self._json(_SESSIONS, {"own": ",".join(own_addresses)})["count"])

    def latest_snapshot(self, account: str, environment: str) -> SnapshotHead | None:
        row = self._json(_SNAPSHOT, {"account": account, "env": environment})
        if row is None:
            return None
        symbols = row.get("symbols") or {}
        return SnapshotHead(
            int(row["event_seq"]), int(row["started_at_ms"]), bool(row["blocked"]),
            {name: {"offered": str(values.get("offered", "0")),
                    "foreign": str(values.get("foreign", "0"))}
             for name, values in sorted(symbols.items())},
            len(row.get("offers") or {}),
            {name: format(sum((Decimal(str(values.get(k, "0")))
                               for k in ("available", "offered", "credits")), Decimal(0)), "f")
             for name, values in sorted(symbols.items())},
        )

    def epoch_authority(self) -> str:
        return str(self._json(_EPOCH)["authority"])

    def observations(self, account: str, environment: str) -> tuple[int, int]:
        row = self._json(_OBSERVATIONS, {"account": account, "env": environment})
        return int(row["seed"]), int(row["runtime"])

    def latest_runtime_basis(self, account: str, environment: str) -> RuntimeBasis | None:
        row = self._json(_RUNTIME_BASIS, {"account": account, "env": environment})
        if row is None:
            return None
        return RuntimeBasis(bool(row["accepted"]), tuple(str(s) for s in row["unexplained"]))

    def trading_state(self, account: str, environment: str) -> str | None:
        state = self._json(_TRADING_STATE, {"account": account, "env": environment})["state"]
        return None if state is None else str(state)

    def runtime_totals(self, account: str, environment: str) -> dict[str, str]:
        row = self._json(_RUNTIME_TOTALS, {"account": account, "env": environment})
        return {str(k): str(v) for k, v in sorted(row.items())}

    def activity_since(self, account: str, environment: str, since_ms: int) -> tuple[int, int]:
        row = self._json(_ACTIVITY, {"account": account, "env": environment,
                                     "since": str(since_ms)})
        return int(row["acked"]), int(row["canceled"])


# --------------------------------------------------------------------------- settings


# Every value bfx-deploy also uses comes from its own Settings: the deploy flock in
# particular must be the very file bfx-deploy locks, or "no deploy runs" would not hold.
_DEPLOY = bfx_deploy.Settings()


@dataclass(frozen=True, slots=True)
class Settings:
    digest: str = ""
    runtime_dir: Path = _DEPLOY.runtime_dir
    state_dir: Path = Path("/var/lib/bfx-ledger-switch")
    lock_file: Path = Path("/run/lock/bfx-ledger-switch.lock")
    deploy_lock_file: Path = _DEPLOY.lock_file
    deploy_command: tuple[str, ...] = ("/usr/local/sbin/bfx-deploy", "--recreate")
    backend_repository: str = _DEPLOY.backend_repository
    network: str = _DEPLOY.network
    postgres_container: str = _DEPLOY.postgres_container
    db_user: str = _DEPLOY.db_user
    db_name: str = _DEPLOY.db_name
    environment: str = "prod"
    cells_path: str = "/app/configs/cells.live.yaml"
    soak_result: Path = Path("/home/ubuntu/bfx/reports/sim-soak/result/soak-result.json")
    container_uid: int = 1000
    dr_current: Path = _DEPLOY.dr_root / "current"
    backup_user: str = _DEPLOY.backup_user
    backup_script: str = bfx_deploy.BACKUP_SCRIPT
    poll_seconds: float = 15.0
    deploy_wait_seconds: float = 1800.0
    snapshot_wait_seconds: float = 600.0
    live_wait_seconds: float = 900.0
    recovery_wait_seconds: float = 900.0
    postgres_wait_seconds: float = 600.0
    check_attempts: int = 3
    check_retry_seconds: float = 30.0
    seed_timeout: float = 900.0


@dataclass(slots=True)
class Evidence:
    """The run's evidence file, rewritten after every step."""

    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    def save(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        bfx_deploy._write_atomic(self.path, json.dumps(self.data, indent=2, sort_keys=True)
                                 + "\n", 0o600)


Notifier = Callable[[str, str], None]
Chown = Callable[[Path, int, int], None]


def _new_run_id(clock: Callable[[], float]) -> str:
    stamp = datetime.fromtimestamp(clock(), UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"switch-{stamp}-{secrets.token_hex(3)}"


def seed_dsn(migrate_env: str) -> tuple[str, dict[str, object]]:
    """The owner DSN of migrate.env and the manifest fields naming it (no value is logged).

    The seed requires an explicit port: libpq's default (5432) is written when absent.
    """
    values = bfx_deploy.parse_env_file(migrate_env, name="migrate.env", compose=False)
    dsn = values.get("DATABASE_URL", "")
    try:
        parts = urllib.parse.urlsplit(dsn)
        port = parts.port
    except ValueError:
        raise SwitchError("migrate_dsn_unusable") from None
    if parts.scheme not in {"postgresql", "postgresql+asyncpg"} or not parts.hostname \
            or not parts.username or not parts.path.strip("/") or parts.query:
        raise SwitchError("migrate_dsn_unusable")
    if port is None:
        parts = parts._replace(netloc=f"{parts.netloc}:5432")
        port = 5432
    database = urllib.parse.unquote(parts.path.lstrip("/"))
    return urllib.parse.urlunsplit(parts), {
        "host": parts.hostname, "port": port, "database": database,
        "user": urllib.parse.unquote(parts.username),
    }


def parse_seed_output(stdout: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lines: list[dict[str, Any]] = []
    for raw in stdout.splitlines():
        if raw.strip():
            try:
                value = json.loads(raw)
            except ValueError:
                raise SwitchError("seed_output_unparsable") from None
            if isinstance(value, dict):
                lines.append(value)
    if not lines or lines[-1].get("kind") != "summary":
        raise SwitchError("seed_output_unparsable")
    return lines[-1], lines


def max_recent_fill_share(lines: Sequence[Mapping[str, Any]]) -> Decimal | None:
    shares: list[Decimal] = []
    for line in lines:
        exposure = line.get("recent_fill")
        if isinstance(exposure, dict) and exposure.get("max_share") is not None:
            try:
                shares.append(Decimal(str(exposure["max_share"])))
            except InvalidOperation:
                raise SwitchError("seed_output_unparsable") from None
    return max(shares) if shares else None


def nav_breaches(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """Symbols whose total capital moved across the halt by more than ``NAV_TOLERANCE``."""
    breaches = []
    for symbol in sorted(set(before) | set(after)):
        old = Decimal(str(before.get(symbol, "0")))
        new = Decimal(str(after.get(symbol, "0")))
        if old == 0 and new == 0:
            continue
        if old == 0 or abs(new - old) / old > NAV_TOLERANCE:
            breaches.append(symbol)
    return breaches


# --------------------------------------------------------------------------- switcher


class Switcher:
    def __init__(
        self, settings: Settings, *, runner: Runner, db: SwitchDb, ledger: Any,
        notify: Notifier, clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        secret_check: Callable[[Path], None] = bfx_deploy.protected_secret_file,
        chown: Chown = os.chown, run_id: str | None = None,
    ) -> None:
        self.settings = settings
        self._runner = runner
        self._db = db
        self._ledger = ledger
        self._notify = notify
        self._clock = clock
        self._sleep = sleep
        self._secret_check = secret_check
        self._chown = chown
        self.run_id = run_id or _new_run_id(clock)
        self.evidence = Evidence(settings.state_dir / self.run_id / "evidence.json")
        self._active_timers: list[str] = []
        self._deploy_lock: IO[str] | None = None
        self.account = ""

    # ------------------------------------------------------------------ plumbing

    @property
    def run_dir(self) -> Path:
        return self.settings.state_dir / self.run_id

    @property
    def image(self) -> str:
        return f"{self.settings.backend_repository}@{self.settings.digest}"

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def _run(self, argv: Sequence[str], *, timeout: float, input_text: str | None = None) -> Any:
        return self._runner(argv, timeout=timeout, input_text=input_text)

    def _check(self, argv: Sequence[str], *, code: str, timeout: float) -> Any:
        try:
            result = self._run(argv, timeout=timeout)
        except CommandError as exc:
            raise SwitchError(f"{code}:{exc.code}") from None
        if result.returncode != 0:
            log(f"{code}: exited {result.returncode}: {result.stderr.strip()[-300:]}")
            raise SwitchError(code)
        return result

    @contextmanager
    def _step(self, name: str) -> Iterator[dict[str, Any]]:
        entry: dict[str, Any] = {"step": name, "start_ms": self._now_ms()}
        self.evidence.data.setdefault("steps", []).append(entry)
        log(f"step {name}")
        try:
            yield entry
        except SwitchError as exc:
            entry.update(end_ms=self._now_ms(), result="failed", detail=exc.code)
            self.evidence.save()
            raise
        except Exception as exc:
            entry.update(end_ms=self._now_ms(), result="failed", detail=type(exc).__name__)
            self.evidence.save()
            raise
        entry.setdefault("result", "ok")
        entry["end_ms"] = self._now_ms()
        self.evidence.save()

    def _say(self, level: str, text: str) -> None:
        log(text)
        self._notify(level, f"ledger switch {self.run_id}: {text}")

    @contextmanager
    def _own_lock(self) -> Iterator[bool]:
        self.settings.lock_file.parent.mkdir(parents=True, exist_ok=True)
        with self.settings.lock_file.open("a") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _wait(self, what: str, limit: float, ready: Callable[[], str | None]) -> None:
        """Poll ``ready`` (None = done, else what is still missing) until ``limit`` seconds."""
        started = self._clock()
        while True:
            missing = ready()
            if missing is None:
                return
            if self._clock() - started >= limit:
                raise SwitchError(f"{what}_timeout:{missing}")
            self._sleep(self.settings.poll_seconds)

    # ------------------------------------------------------------------ entry points

    def preflight(self) -> int:
        with self._own_lock() as acquired:
            if not acquired:
                log("another bfx-ledger-switch run holds the lock")
                return EXIT_USAGE
            self.evidence.data.update(run_id=self.run_id, digest=self.settings.digest,
                                      mode="preflight")
            try:
                self._preflight()
            except SwitchError as exc:
                self.evidence.data["outcome"] = f"precheck_failed:{exc.code}"
                self.evidence.save()
                log(f"P-check failed: {exc.code}")
                return EXIT_PRECHECK
            self.evidence.data["outcome"] = "preflight_ok"
            self.evidence.save()
            log(f"P-checks passed; evidence {self.evidence.path}")
            return EXIT_OK

    def run(self) -> int:
        with self._own_lock() as acquired:
            if not acquired:
                log("another bfx-ledger-switch run holds the lock")
                return EXIT_USAGE
            self.evidence.data.update(run_id=self.run_id, digest=self.settings.digest,
                                      mode="switch", attempts=[])
            try:
                code = self._attempt(1)
                if code == "retry":
                    code = self._attempt(2)
                    if code == "retry":
                        code = EXIT_R1
                return int(code)
            except Exception as exc:  # the last resort: never leave without a word
                reason = exc.code if isinstance(exc, SwitchError | DeployError) else \
                    type(exc).__name__
                self.evidence.data["outcome"] = f"crashed:{reason}"
                self.evidence.save()
                self._say("critical", f"crashed ({reason}); bot state unknown, check "
                                      f"`docker ps` and {self.evidence.path}")
                return EXIT_R2

    # ------------------------------------------------------------------ one attempt

    def _attempt(self, number: int) -> int | str:
        self.evidence.data["attempts"].append({"attempt": number, "start_ms": self._now_ms()})
        try:
            with self._step(f"preflight#{number}"):
                self._preflight()
        except SwitchError as exc:
            self.evidence.data["outcome"] = f"precheck_failed:{exc.code}"
            self.evidence.save()
            self._say("warning", f"not started, P-check failed ({exc.code}); nothing was "
                                 "stopped, legacy keeps running")
            return EXIT_PRECHECK
        try:
            try:
                self._halt_and_seed(number)
            except Exception as exc:
                return self._before_commit_failed(_as_switch_error(exc), number)
            try:
                self._go_live()
            except Exception as exc:
                return self._after_commit_failed(_as_switch_error(exc))
            self.evidence.data["outcome"] = "switched"
            self.evidence.save()
        finally:
            self._release_deploy_lock()
            self._restart_timers()
        self._say("info", "done: capital authority is ledger, first runtime basis accepted, "
                          f"trading ACTIVE, web API ready; evidence {self.evidence.path}")
        return EXIT_OK

    def _halt_and_seed(self, number: int) -> None:
        with self._step("stop_timers") as entry:
            self._stop_timers()
            entry["stopped"] = list(self._active_timers)
        with self._step("deploy_lock"):
            self._take_deploy_lock()
        with self._step("record_resting") as entry:
            entry["resting"] = self._record_resting()
        with self._step("stop_webapi") as entry:
            self._stop_container("bfx-webapi")
            stopped_at = self._now_ms()
            entry["stopped_at_ms"] = stopped_at
        with self._step("wait_legacy_snapshot") as entry:
            entry["snapshot_event_seq"] = self._wait_snapshot_after(stopped_at)
        with self._step("stop_bot"):
            self._stop_container("bfx-bot")
            self.evidence.data["bot_stopped"] = True
        with self._step("backup") as entry:
            entry["label"] = self.evidence.data["backup_label"] = self._backup("diff")
        with self._step("seed_switch") as entry:
            summary, lines = self._run_seed("switch", f"{self.run_id}-a{number}")
            entry["seed_summary"] = summary
            if summary.get("exit_code") != 0 or summary.get("committed") is not True:
                raise SwitchError(f"seed_refused:{summary.get('reason', summary.get('exit_code'))}",
                                  reason=str(summary.get("reason") or ""))
            if self._db.epoch_authority() != "ledger":
                raise SwitchError("seed_reported_commit_but_epoch_is_legacy")
            share = max_recent_fill_share(lines)
            entry["recent_fill_max_share"] = None if share is None else format(share, "f")
        self.evidence.data["seed_committed"] = True
        if share is not None and share > F7_NOTIFY_SHARE:
            self._say("warning", f"F7: live credits still recent_fill are {share:.2%} of a "
                                 "symbol's capital (> 20 %); they stay multi-cell until they "
                                 "end. Switch continues.")

    def _go_live(self) -> None:
        with self._step("recreate"):
            before = self._ledger_view().last_attempt
            self._release_deploy_lock()
            self._check(list(self.settings.deploy_command), code="recreate_failed",
                        timeout=3600.0)
            last = self._ledger_view().last_attempt
            if last is None or (before is not None and last.id <= before.id) \
                    or last.outcome != "deployed" or last.backend_digest != self.settings.digest:
                raise SwitchError("recreate_not_recorded_as_deployed")
        with self._step("wait_ledger_live"):
            self._wait("ledger_live", self.settings.live_wait_seconds, self._live_missing)
        self.evidence.data["switched_at_ms"] = self._now_ms()
        self._report_only("nav_continuity", self._nav_continuity)
        self._report_only("schedule_watch", self._schedule_watch)

    def _report_only(self, name: str, check: Callable[[dict[str, Any]], None]) -> None:
        """A post-switch check: recorded and notified, never a failure of the switch."""
        try:
            with self._step(name) as entry:
                check(entry)
        except Exception as exc:
            code = _as_switch_error(exc).code
            self._say("warning", f"post-switch check {name} could not run ({code}); the switch "
                                 "itself is done")

    def _nav_continuity(self, entry: dict[str, Any]) -> None:
        before = (self.evidence.data.get("resting") or {}).get("total_capital") or {}
        after = self._db.runtime_totals(self.account, self.settings.environment)
        breaches = nav_breaches(before, after)
        entry.update(legacy=before, ledger=after, breaches=breaches,
                     tolerance=format(NAV_TOLERANCE, "f"))
        if breaches:
            entry["result"] = "breach"
            self._say("warning", "NAV continuity: the first runtime basis differs from the last "
                                 f"legacy snapshot by more than {NAV_TOLERANCE:.1%} on "
                                 f"{', '.join(breaches)} (legacy {before}, ledger {after}); "
                                 "report only, check deposits/withdrawals and the basis")

    def _schedule_watch(self, entry: dict[str, Any]) -> None:
        unit = f"bfx-ledger-switch-watch-{self.run_id}"
        argv = ["systemd-run", "--unit", unit, f"--on-active={WATCH_DELAY}",
                "--timer-property=AccuracySec=5min", "--property=Environment=HOME=/root",
                sys.executable, str(Path(__file__).resolve()), "watch", "--run-id", self.run_id]
        self._check(argv, code="watch_schedule_failed", timeout=60.0)
        entry["unit"] = unit

    # ------------------------------------------------------------------ failure branches

    def _before_commit_failed(self, exc: SwitchError, number: int) -> int | str:
        try:
            committed = self._db.epoch_authority() == "ledger"
        except SwitchError:
            committed = None
        if committed is None:
            return self._r2(exc, "the database could not be read to tell whether the seed "
                                 "committed")
        if committed:
            return self._r2(exc, "")
        restarted = self._r1(exc)
        retry = exc.reason in TRANSIENT_REFUSALS and number == 1 and restarted
        if retry:
            try:
                with self._step("wait_legacy_recovery"):
                    started = self._now_ms()
                    self._wait("legacy_recovery", self.settings.recovery_wait_seconds,
                               lambda: self._snapshot_missing(started))
            except SwitchError as wait:
                self._say("critical", f"R1: legacy restarted but its recovery did not settle "
                                      f"({wait.code}); no retry")
                return EXIT_R1
            self._say("info", f"R1: retrying the whole run once after legacy recovery "
                              f"({exc.reason})")
            return "retry"
        return EXIT_R1

    def _r1(self, exc: SwitchError) -> bool:
        self.evidence.data["outcome"] = f"r1:{exc.code}"
        problems: list[str] = []
        with self._step("r1_start_legacy") as entry:
            self._release_deploy_lock()
            for name in ("bfx-bot", "bfx-webapi"):
                try:
                    self._check(["docker", "start", name], code=f"start_failed:{name}",
                                timeout=120.0)
                except SwitchError as start:
                    problems.append(start.code)
            entry["problems"] = problems
        self._restart_timers()
        if problems:
            self._say("critical", f"R1: failed before the seed committed ({exc.code}); legacy "
                                  f"restart FAILED ({', '.join(problems)}); the database is "
                                  "still legacy")
            return False
        self._say("warning", f"R1: failed before the seed committed ({exc.code}); nothing was "
                             "written; bot and web API restarted on legacy, timers restored")
        return True

    def _exposure(self) -> str:
        resting = self.evidence.data.get("resting") or {}
        return f"resting at halt start: {json.dumps(resting, sort_keys=True)}"

    def _r2(self, exc: SwitchError, why: str) -> int:
        self.evidence.data["outcome"] = f"r2:{exc.code}"
        with self._step("r2_stay_halted") as entry:
            try:
                result = self._run(["docker", "stop", "bfx-bot"], timeout=180.0)
                stopped = result.returncode == 0 or "no such container" in result.stderr.lower()
            except CommandError:
                stopped = False
            entry["bot_stopped"] = stopped
        self._say("critical", (
            f"R2: the seed committed (epoch ledger) but the switch failed before the first "
            f"runtime ledger write ({exc.code}{'; ' + why if why else ''}). Bot stays stopped"
            f"{'' if entry['bot_stopped'] else ' (docker stop bfx-bot FAILED: stop it)'} "
            f"({self._exposure()}). Options: forward-fix (fix and release, or --recreate after "
            f"a config fix), or restore the halt backup in place: "
            f"`bfx_ledger_switch.py restore-halt-backup --run-id {self.run_id}` (label "
            f"{self.evidence.data.get('backup_label')}; only before any runtime ledger write). "
            f"Evidence {self.evidence.path}"))
        return EXIT_R2

    def _after_commit_failed(self, exc: SwitchError) -> int:
        try:
            _, runtime = self._db.observations(self.account, self.settings.environment)
        except SwitchError:
            runtime = -1
        if runtime == 0:
            return self._r2(exc, "")
        self.evidence.data["outcome"] = f"r3:{exc.code}"
        self.evidence.save()
        self._say("critical", (
            f"R3: the ledger runtime already wrote ({'unknown count' if runtime < 0 else runtime} "
            f"runtime observations) and the switch did not complete ({exc.code}). Forward-fix "
            f"only; protection halts by itself on an unexplained exposure. "
            f"Evidence {self.evidence.path}"))
        return EXIT_R3

    # ------------------------------------------------------------------ P-checks

    def _preflight(self) -> None:
        settings = self.settings
        if bfx_deploy.DIGEST.fullmatch(settings.digest) is None:
            raise SwitchError("digest_invalid")
        self.account = self._account()
        self.evidence.data["account"] = self.account
        view = self._ledger_view()
        last, success = view.last_attempt, view.last_success
        if last is None or success is None or last.id != success.id \
                or last.outcome != "deployed":
            raise SwitchError("last_deploy_not_successful")
        if success.backend_digest != settings.digest:
            raise SwitchError("running_digest_is_not_the_soaked_digest")
        self._require_soak_pass(success.source_revision)
        self._require_running_digest()
        current, heads = self._schema()
        if not heads or set(current) != set(heads):
            raise SwitchError("schema_not_at_head")
        state = self._run(["systemctl", "is-active", WEEKLY_SERVICE], timeout=30.0)
        if state.stdout.strip() in RUNNING_STATES:
            raise SwitchError("weekly_report_running")
        names = self._check(["docker", "ps", "--format", "{{.Names}}"], code="docker_ps_failed",
                            timeout=60.0).stdout.split()
        if any(name == SIM_CONTAINER or name.startswith(SIM_CONTAINER + "-") for name in names):
            raise SwitchError("simulation_running")
        own = self._container_addresses(("bfx-bot", "bfx-webapi"))
        if self._db.foreign_runtime_sessions(own):
            raise SwitchError("runtime_session_present")
        self._seed_check()

    def _require_soak_pass(self, revision: str) -> None:
        """The soak's result file: PASS, on this digest, its last generation this revision."""
        try:
            result = json.loads(self.settings.soak_result.read_text(encoding="utf-8"))
            verdict, digest = result["verdict"], result["image_digest"]
            last = result["last_service_version"]
        except (OSError, ValueError, KeyError, TypeError):
            raise SwitchError("soak_result_missing") from None
        if verdict != "PASS":
            raise SwitchError("soak_result_not_pass")
        if digest != self.settings.digest:
            raise SwitchError("soak_result_digest_mismatch")
        if last != revision:
            raise SwitchError("soak_result_revision_mismatch")
        self.evidence.data["soak_result"] = {
            "path": str(self.settings.soak_result), "window": result.get("window"),
            "report_sha256": result.get("report_sha256")}

    def _ledger_view(self) -> Any:
        try:
            return self._ledger.read()
        except DeployError as exc:
            raise SwitchError(f"deploy_ledger_unreadable:{exc.code}") from None

    def _account(self) -> str:
        path = self.settings.runtime_dir / "bot.env"
        try:
            self._secret_check(path)
            values = bfx_deploy.parse_env_file(path.read_text(encoding="utf-8"),
                                               name="bot.env", compose=False)
            return str(UUID(values["BFX_EXCHANGE_ACCOUNT_ID"]))
        except (DeployError, OSError, KeyError, ValueError):
            raise SwitchError("account_id_unavailable") from None

    def _inspect(self, names: Sequence[str]) -> dict[str, Any]:
        result = self._check(["docker", "inspect", "--type", "container", *names],
                             code="docker_inspect_failed", timeout=60.0)
        try:
            return {str(r["Name"]).lstrip("/"): r for r in json.loads(result.stdout)}
        except (ValueError, KeyError, TypeError):
            raise SwitchError("docker_inspect_unparsable") from None

    def _require_running_digest(self) -> None:
        records = self._inspect(("bfx-bot", "bfx-webapi"))
        for name in ("bfx-bot", "bfx-webapi"):
            record = records.get(name) or {}
            if (record.get("Config") or {}).get("Image") != self.image:
                raise SwitchError(f"running_digest_mismatch:{name}")
            if not (record.get("State") or {}).get("Running"):
                raise SwitchError(f"legacy_not_running:{name}")

    def _container_addresses(self, names: Sequence[str]) -> list[str]:
        records = self._inspect(names)
        addresses: set[str] = set()
        for record in records.values():
            for network in ((record.get("NetworkSettings") or {}).get("Networks") or {}).values():
                address = (network or {}).get("IPAddress")
                if address:
                    addresses.add(str(address))
        return sorted(addresses)

    def _one_shot(self, args: Sequence[str], *, name: str, timeout: float,
                  mounts: Sequence[str] = (), env_file: Path | None = None) -> Any:
        argv = ["docker", "run", "--rm", "--name", name, "--pull=never", "--read-only",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                "--user", f"{self.settings.container_uid}:{self.settings.container_uid}",
                "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--network", self.settings.network, "--workdir", "/app", "--entrypoint", "",
                "--env", "PYTHONDONTWRITEBYTECODE=1"]
        for mount in mounts:
            argv += ["--volume", mount]
        if env_file is not None:
            argv += ["--env-file", str(env_file)]
        argv += [self.image, *args]
        try:
            return self._run(argv, timeout=timeout)
        except CommandError as exc:
            if exc.timed_out:
                self._run(["docker", "rm", "--force", name], timeout=60.0)
            raise SwitchError(f"one_shot_failed:{exc.code}") from None

    def _schema(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        env_file = self.settings.runtime_dir / "migrate.env"
        try:
            self._secret_check(env_file)
        except DeployError as exc:
            raise SwitchError(exc.code) from None
        revisions = []
        for which in ("current", "heads"):
            result = self._one_shot(("/app/.venv/bin/alembic", which),
                                    name=f"bfx-ledger-switch-alembic-{secrets.token_hex(4)}",
                                    timeout=180.0, env_file=env_file)
            if result.returncode != 0:
                raise SwitchError(f"alembic_{which}_failed")
            try:
                revisions.append(bfx_deploy._alembic_revisions(result.stdout))
            except DeployError as exc:
                raise SwitchError(exc.code) from None
        return revisions[0], revisions[1]

    def _seed_check(self) -> None:
        reasons: list[str] = []
        for attempt in range(1, self.settings.check_attempts + 1):
            summary, lines = self._run_seed("check", f"{self.run_id}-check{attempt}")
            if summary.get("exit_code") == 0:
                share = max_recent_fill_share(lines)
                self.evidence.data["check_recent_fill_max_share"] = (
                    None if share is None else format(share, "f"))
                return
            refusals = summary.get("refusals")
            if not isinstance(refusals, list) or not refusals:
                raise SwitchError(f"seed_check_failed:{summary.get('reason', 'unknown')}")
            reasons = sorted({str(r.get("reason")) for r in refusals if isinstance(r, dict)})
            if not set(reasons) <= TRANSIENT_REFUSALS or attempt == self.settings.check_attempts:
                break
            self._sleep(self.settings.check_retry_seconds)
        raise SwitchError("seed_check_refused:" + ",".join(reasons))

    # ------------------------------------------------------------------ seed

    def _write_seed_input(self, seed_run_id: str) -> Path:
        env_file = self.settings.runtime_dir / "migrate.env"
        try:
            self._secret_check(env_file)
            text = env_file.read_text(encoding="utf-8")
        except DeployError as exc:
            raise SwitchError(exc.code) from None
        except OSError:
            raise SwitchError("migrate_env_unreadable") from None
        dsn, fields = seed_dsn(text)
        manifest = {"mode": "seed", "run_id": seed_run_id, **fields,
                    "realm": self.settings.environment,
                    "scopes": [f"{self.account}:{self.settings.environment}"]}
        directory = self.run_dir / "seed-input"
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(mode=0o700, parents=True)
        uid = self.settings.container_uid
        # File to file only; the container user owns the directory and reads the 0600 DSN.
        bfx_deploy._write_atomic(directory / "dsn", dsn + "\n", 0o600)
        bfx_deploy._write_atomic(directory / "manifest.json", json.dumps(manifest) + "\n", 0o600)
        for path in (directory, directory / "dsn", directory / "manifest.json"):
            self._chown(path, uid, uid)
        return directory

    def _run_seed(self, mode: str, seed_run_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        directory = self._write_seed_input(seed_run_id)
        args = ["/app/.venv/bin/python", "-m", SEED_MODULE, f"--{mode}",
                "--dsn-file", f"{SEED_INPUT_MOUNT}/dsn",
                "--manifest", f"{SEED_INPUT_MOUNT}/manifest.json",
                "--run-id", seed_run_id,
                "--scope", f"{self.account}:{self.settings.environment}",
                "--cells", self.settings.cells_path]
        if mode == "switch":
            args.append("--authorize-seed")
        try:
            result = self._one_shot(args, name=f"bfx-ledger-switch-{mode}-{secrets.token_hex(4)}",
                                    timeout=self.settings.seed_timeout,
                                    mounts=(f"{directory}:{SEED_INPUT_MOUNT}:ro",))
        finally:
            shutil.rmtree(directory, ignore_errors=True)
        output = self.run_dir / f"seed-{seed_run_id}.jsonl"
        bfx_deploy._write_atomic(output, result.stdout, 0o600)
        return parse_seed_output(result.stdout)

    # ------------------------------------------------------------------ steps

    def _stop_timers(self) -> None:
        for timer in TIMERS:
            if self._run(["systemctl", "is-active", timer], timeout=30.0).stdout.strip() \
                    == "active":
                self._active_timers.append(timer)
        for timer in self._active_timers:
            self._check(["systemctl", "stop", timer], code=f"timer_stop_failed:{timer}",
                        timeout=60.0)

    def _restart_timers(self) -> None:
        failed = []
        for timer in self._active_timers:
            try:
                self._check(["systemctl", "start", timer], code=f"timer_start_failed:{timer}",
                            timeout=60.0)
            except SwitchError as exc:
                failed.append(exc.code)
        self.evidence.data["timers_restarted"] = [t for t in self._active_timers
                                                  if f"timer_start_failed:{t}" not in failed]
        self.evidence.save()
        if failed:
            self._say("warning", f"could not restart timers: {', '.join(failed)}")
        self._active_timers = []

    def _take_deploy_lock(self) -> None:
        path = self.settings.deploy_lock_file
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a")

        def free() -> str | None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "bfx_deploy_running"
            return None

        try:
            self._wait("deploy_lock", self.settings.deploy_wait_seconds, free)
        except BaseException:
            handle.close()
            raise
        self._deploy_lock = handle

    def _release_deploy_lock(self) -> None:
        if self._deploy_lock is not None:
            fcntl.flock(self._deploy_lock.fileno(), fcntl.LOCK_UN)
            self._deploy_lock.close()
            self._deploy_lock = None

    def _record_resting(self) -> dict[str, Any]:
        head = self._db.latest_snapshot(self.account, self.settings.environment)
        if head is None:
            raise SwitchError("legacy_snapshot_missing")
        resting = {"snapshot_event_seq": head.event_seq, "offers": head.offers,
                   "symbols": {k: dict(v) for k, v in head.resting.items()},
                   "total_capital": dict(head.totals)}
        self.evidence.data["resting"] = resting
        return resting

    def _stop_container(self, name: str) -> None:
        self._check(["docker", "stop", name], code=f"stop_failed:{name}", timeout=180.0)

    def _snapshot_missing(self, after_ms: int) -> str | None:
        head = self._db.latest_snapshot(self.account, self.settings.environment)
        if head is None or head.query_started_at_ms <= after_ms:
            return "no_snapshot_after_stop"
        if head.blocked:
            return "latest_snapshot_blocked"
        return None

    def _wait_snapshot_after(self, after_ms: int) -> int:
        self._wait("legacy_snapshot", self.settings.snapshot_wait_seconds,
                   lambda: self._snapshot_missing(after_ms))
        head = self._db.latest_snapshot(self.account, self.settings.environment)
        assert head is not None
        return head.event_seq

    def _backup_label(self) -> str | None:
        result = self._check(["docker", "exec", "--user", "postgres",
                              self.settings.postgres_container, "pgbackrest", "--stanza=bfx",
                              "--output=json", "info"], code="pgbackrest_info_failed",
                             timeout=300.0)
        try:
            backups = json.loads(result.stdout)[0].get("backup") or []
            return str(backups[-1]["label"]) if backups else None
        except (ValueError, KeyError, TypeError, IndexError):
            raise SwitchError("pgbackrest_info_unparsable") from None

    def _backup(self, kind: str) -> str:
        before = self._backup_label()
        script = self.settings.dr_current / self.settings.backup_script
        self._check(["runuser", "-u", self.settings.backup_user, "--", str(script),
                     "--type", kind], code="backup_failed", timeout=7200.0)
        label = self._backup_label()
        if label is None or label == before:
            raise SwitchError("backup_label_unchanged")
        return label

    def _probe_ready(self) -> bool:
        result = self._run(["docker", "exec", "bfx-webapi", "/app/.venv/bin/python", "-c",
                            PROBE, "http://127.0.0.1:8000/ready"], timeout=20.0)
        return bool(result.returncode == 0)

    def _live_missing(self) -> str | None:
        account, environment = self.account, self.settings.environment
        missing = []
        _, runtime = self._db.observations(account, environment)
        if runtime == 0:
            missing.append("runtime_observation")
        basis = self._db.latest_runtime_basis(account, environment)
        if basis is None or not basis.accepted:
            missing.append("runtime_basis")
        elif basis.unexplained:
            missing.append("unexplained_lending:" + "+".join(basis.unexplained))
        state = self._db.trading_state(account, environment)
        if state not in (None, "ACTIVE"):
            missing.append(f"trading_state_{state}")
        if not self._probe_ready():
            missing.append("webapi_ready")
        return ",".join(missing) or None

    # ------------------------------------------------------------------ post-switch watch

    def watch(self) -> int:
        """24 h after the switch (transient timer): notify when the ledger runtime neither
        acknowledged a submit nor repriced since the switch. Report only."""
        try:
            switch = json.loads(self.evidence.path.read_text(encoding="utf-8"))
            account, since = str(UUID(switch["account"])), int(switch["switched_at_ms"])
        except (OSError, ValueError, KeyError, TypeError):
            log(f"no usable evidence at {self.evidence.path}")
            return EXIT_USAGE
        acked, canceled = self._db.activity_since(account, self.settings.environment, since)
        report = {"run_id": self.run_id, "since_ms": since, "acked_submits": acked,
                  "repriced_or_canceled": canceled}
        Evidence(self.run_dir / "watch.json", report).save()
        if acked == 0 and canceled == 0:
            self._say("warning", f"post-switch watch: no acknowledged submit and no reprice in "
                                 f"the {WATCH_DELAY} since the switch; check the policy, the "
                                 "cells and /admin/trading-status (report only)")
        else:
            log(f"post-switch watch: {acked} acked submits, {canceled} repriced/canceled")
        return EXIT_OK

    # ------------------------------------------------------------------ restore

    def restore_halt_backup(self) -> int:
        with self._own_lock() as acquired:
            if not acquired:
                log("another bfx-ledger-switch run holds the lock")
                return EXIT_USAGE
            source = self.evidence.path
            try:
                switch = json.loads(source.read_text(encoding="utf-8"))
                label, account = str(switch["backup_label"]), str(UUID(switch["account"]))
            except (OSError, ValueError, KeyError, TypeError):
                log(f"no usable evidence at {source}")
                return EXIT_RESTORE_REFUSED
            self.account = account
            self.evidence = Evidence(self.run_dir / "restore.json",
                                     {"run_id": self.run_id, "label": label})
            try:
                self._restore_preconditions()
            except SwitchError as exc:
                self.evidence.data["outcome"] = f"refused:{exc.code}"
                self.evidence.save()
                self._say("warning", f"restore-halt-backup refused ({exc.code}); nothing done")
                return EXIT_RESTORE_REFUSED
            try:
                return self._restore(label)
            finally:
                self._release_deploy_lock()
                self._restart_timers()

    def _restore_preconditions(self) -> None:
        if self._db.epoch_authority() != "ledger":
            raise SwitchError("seed_not_committed")
        _, runtime = self._db.observations(self.account, self.settings.environment)
        if runtime:
            raise SwitchError("runtime_ledger_write_exists")

    def _restore(self, label: str) -> int:
        try:
            with self._step("stop_timers"):
                self._stop_timers()
            with self._step("deploy_lock"):
                self._take_deploy_lock()
            with self._step("stop_application"):
                for name in ("bfx-bot", "bfx-webapi", "bfx-frontend"):
                    result = self._run(["docker", "stop", name], timeout=180.0)
                    if result.returncode != 0 and "no such container" not in result.stderr.lower():
                        raise SwitchError(f"stop_failed:{name}")
            with self._step("recheck"):
                self._restore_preconditions()  # no writer can have raced the first check
        except SwitchError as exc:
            self.evidence.data["outcome"] = f"refused:{exc.code}"
            self.evidence.save()
            self._say("critical", f"restore-halt-backup stopped before touching postgres "
                                  f"({exc.code}); the application may be stopped")
            return EXIT_RESTORE_REFUSED
        try:
            with self._step("stop_postgres") as entry:
                image = (self._inspect((self.settings.postgres_container,))
                         .get(self.settings.postgres_container, {}).get("Config") or {}).get("Image")
                if not image:
                    raise SwitchError("postgres_image_unknown")
                entry["image"] = image
                self._check(["docker", "stop", self.settings.postgres_container],
                            code="postgres_stop_failed", timeout=300.0)
            with self._step("pgbackrest_restore"):
                self._check(["docker", "run", "--rm",
                             "--name", f"bfx-ledger-switch-restore-{secrets.token_hex(4)}",
                             "--volumes-from", self.settings.postgres_container,
                             "--user", "postgres", "--entrypoint", "pgbackrest", str(image),
                             "--stanza=bfx", "--delta", f"--set={label}", "--type=immediate",
                             "--target-action=promote", "restore"],
                            code="pgbackrest_restore_failed", timeout=7200.0)
        except SwitchError as exc:
            self.evidence.data["outcome"] = f"failed:{exc.code}"
            self.evidence.save()
            self._say("critical", f"restore-halt-backup FAILED ({exc.code}); postgres may be "
                                  "stopped; follow docs/runbooks/offsite-dr.md")
            return EXIT_RESTORE_FAILED
        try:
            with self._step("start_postgres"):
                self._check(["docker", "start", self.settings.postgres_container],
                            code="postgres_start_failed", timeout=120.0)
                self._wait("postgres", self.settings.postgres_wait_seconds, self._pg_missing)
            with self._step("verify_legacy"):
                seed, runtime = self._db.observations(self.account, self.settings.environment)
                if self._db.epoch_authority() != "legacy" or seed or runtime:
                    raise SwitchError("restored_state_not_legacy")
            with self._step("fresh_backup") as entry:
                entry["label"] = self._backup("full")
            with self._step("start_legacy"):
                for name in ("bfx-bot", "bfx-webapi", "bfx-frontend"):
                    self._check(["docker", "start", name], code=f"start_failed:{name}",
                                timeout=120.0)
        except SwitchError as exc:
            self.evidence.data["outcome"] = f"failed:{exc.code}"
            self.evidence.save()
            self._say("critical", f"restore-halt-backup restored {label} but then failed "
                                  f"({exc.code}); check postgres and {self.evidence.path}")
            return EXIT_RESTORE_FAILED
        self.evidence.data["outcome"] = "restored"
        self.evidence.save()
        self._say("info", f"restored the halt backup {label} in place, took a fresh full "
                          "backup, legacy restarted; the database is legacy again")
        return EXIT_OK

    def _pg_missing(self) -> str | None:
        result = self._run(["docker", "exec", self.settings.postgres_container, "pg_isready",
                            "-h", "/var/run/postgresql"], timeout=30.0)
        return None if result.returncode == 0 else "not_ready"


# --------------------------------------------------------------------------- CLI


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "preflight"):
        sub = commands.add_parser(name)
        sub.add_argument("--digest", required=True,
                         help="the backend digest the soak passed on (sha256:...)")
        sub.add_argument("--soak-result", type=Path, default=Settings().soak_result,
                         help="the soak's result file (sim_soak_report.py --result-out)")
    for name in ("restore-halt-backup", "watch"):
        commands.add_parser(name).add_argument("--run-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if os.geteuid() != 0:
        log("must run as root (reads /opt/bfx/runtime, drives docker and systemd)")
        return EXIT_USAGE
    signal.signal(signal.SIGTERM, _terminate)
    defaults = Settings()
    settings = Settings(digest=getattr(args, "digest", "") or "",
                        soak_result=getattr(args, "soak_result", None) or defaults.soak_result)
    runner = bfx_deploy.subprocess_runner
    notify_config = defaults.runtime_dir / "notify.env"

    def notify(level: str, text: str) -> None:
        bfx_notify.send(text, level=level, config_path=notify_config)

    switcher = Switcher(
        settings, runner=runner,
        db=PsqlSwitchDb(runner, container=settings.postgres_container, db_user=settings.db_user,
                        db_name=settings.db_name),
        ledger=bfx_deploy.PsqlLedger(runner, container=settings.postgres_container,
                                     db_user=settings.db_user, db_name=settings.db_name),
        notify=notify, run_id=getattr(args, "run_id", None),
    )
    if args.command == "restore-halt-backup":
        return switcher.restore_halt_backup()
    if args.command == "watch":
        return switcher.watch()
    if args.command == "preflight":
        return switcher.preflight()
    return switcher.run()


if __name__ == "__main__":
    raise SystemExit(main())
