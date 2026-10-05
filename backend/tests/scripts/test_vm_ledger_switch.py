"""bfx_ledger_switch against a simulated VM (no Docker, no systemd, no database).

The fake host answers the argv the tool issues (systemctl, docker inspect/ps/run/stop/start/
exec, runuser backup.sh, bfx-deploy --recreate) and a fake database answers its reads. A fake
clock advances only on sleep, so every bounded wait ends deterministically.

Proved here: every P-check failure ends the run before anything is stopped; the timers are
stopped before the web API (and restarted on every exit path); each failure takes its branch
(R1 restarts legacy, R2 keeps the bot stopped and names restore-halt-backup, R3 forward-fix
only); the F6 / query-pending refusal retries the whole run once after legacy recovery;
restore-halt-backup refuses after a runtime ledger observation.

Mutations (apply one at a time, run this file, revert; the test that fails is named):

* ``_halt_and_seed`` skips ``_stop_timers``: ``test_the_switch_succeeds_end_to_end`` (order);
* ``_preflight`` skips the bfx-sim container check:
  ``test_every_p_check_failure_stops_nothing[simulation_running]``;
* ``_restore_preconditions`` skips the runtime-observation check:
  ``test_restore_refuses_after_a_runtime_ledger_observation``;
* ``_attempt`` without the ``finally`` restart: ``test_r2_keeps_the_bot_stopped_and_names_the_restore``.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


switch = _load("vm_ops_bfx_ledger_switch_under_test", ROOT / "deploy/vm/ops/bfx_ledger_switch.py")
bfx = switch.bfx_deploy

REPO = "ghcr.io/will413028/bfx-funding-bot-backend"
DIGEST = "sha256:" + "3" * 64
OTHER = "sha256:" + "9" * 64
ACCOUNT = "4f9c2d64-8a52-4c4f-9a61-0d4f3a3b1e10"
DSN_PASSWORD = "s3cr3t-owner-password"
HEAD = "c6d7e8f9a0b1"
INITIAL_TIMERS = {"bfx-deploy.timer": "active", "bfx-weekly-report.timer": "active",
                  "bfx-restore-test.timer": "active", "bfx-pgbackrest-backup.timer": "inactive"}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeDb:
    clock: FakeClock
    host: Any = None
    epoch: str = "legacy"
    seed_observations: int = 0
    writes: dict[str, int] = field(default_factory=dict)
    actor: str = "migration"
    late_commit: str | None = None  # a seed run id whose transaction commits after the signal
    owner_polls: int = 0
    owner_users: list[str] = field(default_factory=list)
    unexplained: tuple[str, ...] = ()
    basis: bool = False
    state: str | None = "ACTIVE"
    foreign_sessions: int = 0
    produce_snapshots: bool = True
    last_started_ms: int = 0
    unreadable: bool = False
    seen_addresses: list[Sequence[str]] = field(default_factory=list)
    ledger_totals: dict[str, str] = field(default_factory=lambda: {"fUST": "5003.5"})
    activity: tuple[int, int] = (0, 0)
    activity_asked: list[int] = field(default_factory=list)

    def runtime_totals(self, account: str, environment: str) -> dict[str, str]:
        return dict(self.ledger_totals)

    def activity_since(self, account: str, environment: str, since_ms: int) -> tuple[int, int]:
        self.activity_asked.append(since_ms)
        return self.activity

    def _guard(self) -> None:
        if self.unreadable:
            raise switch.SwitchError("psql_exit_2")

    def foreign_runtime_sessions(self, own_addresses: Sequence[str]) -> int:
        self.seen_addresses.append(tuple(own_addresses))
        return self.foreign_sessions

    def latest_snapshot(self, account: str, environment: str) -> Any:
        assert (account, environment) == (ACCOUNT, "prod")
        if self.produce_snapshots and self.host.running.get("bfx-bot"):
            self.last_started_ms = int(self.clock() * 1000)
        return switch.SnapshotHead(41, self.last_started_ms, False,
                                   {"fUST": {"offered": "1200", "foreign": "75"}}, 3,
                                   {"fUST": "5000"})

    def epoch_authority(self) -> str:
        self._guard()
        return self.epoch

    def epoch_actor(self) -> str:
        self._guard()
        return self.actor

    def commit_seed(self, seed_run_id: str) -> None:
        self.epoch, self.seed_observations = "ledger", 1
        self.actor = f"ledger_seed:{seed_run_id}"

    def boundary(self, account: str, environment: str) -> Any:
        self._guard()
        return switch.Boundary(self.seed_observations, dict(self.writes))

    def owner_transactions(self, user: str) -> int:
        """A late seed: its transaction is still open at the first look, then commits."""
        self.owner_polls += 1
        self.owner_users.append(user)
        if self.late_commit is not None:
            self.commit_seed(self.late_commit)
            self.late_commit = None
            return 1
        return 0

    def latest_runtime_basis(self, account: str, environment: str) -> Any:
        return switch.RuntimeBasis(True, self.unexplained) if self.basis else None

    def trading_state(self, account: str, environment: str) -> str | None:
        return self.state


class FakeLedger:
    def __init__(self) -> None:
        self.rows: list[Any] = []
        self.add("deployed", DIGEST)

    def add(self, outcome: str, digest: str) -> None:
        self.rows.append(bfx.LedgerRow(
            id=len(self.rows) + 1, attempt_id=f"a{len(self.rows)}", source_revision="b" * 40,
            backend_digest=digest, frontend_digest="sha256:" + "4" * 64, outcome=outcome,
            migrations_applied=False))

    def read(self) -> Any:
        successes = [row for row in self.rows if row.outcome == "deployed"]
        return bfx.LedgerView(exists=True, last_attempt=self.rows[-1],
                              last_success=successes[-1] if successes else None)


def seed_lines(mode: str, *, reason: str | None = None, share: str = "0.010000") -> str:
    scope = {"account_id": ACCOUNT, "environment": "prod"}
    exposure = {"symbols": {"fUST": {"recent_fill": "12", "total_capital": "1200",
                                     "share": share}}, "max_share": share}
    if mode == "check":
        refusals = [] if reason is None else [{"scope": scope, "reason": reason, "detail": []}]
        lines = [{"kind": "check", "scope": scope, "refusal": refusals[0] if refusals else None,
                  "recent_fill": exposure},
                 {"kind": "summary", "mode": "check", "exit_code": 3 if reason else 0,
                  "committed": False, "refusals": refusals}]
    elif reason is None:
        lines = [{"kind": "seed", "scope": scope, "recent_fill": exposure},
                 {"kind": "summary", "mode": "switch", "exit_code": 0, "committed": True}]
    else:
        lines = [{"kind": "summary", "exit_code": 3, "reason": reason, "detail": [],
                  "committed": False}]
    return "".join(json.dumps(line) + "\n" for line in lines)


@dataclass
class FakeHost:
    clock: FakeClock
    db: FakeDb
    ledger: FakeLedger
    timers: dict[str, str] = field(default_factory=lambda: dict(INITIAL_TIMERS))
    weekly: str = "inactive"
    running: dict[str, bool] = field(default_factory=lambda: {
        "bfx-bot": True, "bfx-webapi": True, "bfx-frontend": True, "bfx-postgres": True})
    images: dict[str, str] = field(default_factory=lambda: {
        "bfx-bot": f"{REPO}@{DIGEST}", "bfx-webapi": f"{REPO}@{DIGEST}",
        "bfx-postgres": "bfx-postgres:local"})
    extra_containers: list[str] = field(default_factory=list)
    current: str = HEAD
    heads: str = HEAD
    checks: list[str] = field(default_factory=list)  # refusal reasons per --check call; None ok
    switches: list[Any] = field(default_factory=list)  # per --switch call: None ok / reason / exc
    share: str = "0.010000"
    backup_fails: bool = False
    recreate_ok: bool = True
    recreate_failure_writes: dict[str, int] = field(default_factory=dict)
    break_db_at: str | None = None  # "backup" | "seed": the database becomes unreadable there
    no_ip: bool = False
    recreate_writes: bool = True
    ready: bool = True
    restore_ok: bool = True
    labels: list[str] = field(default_factory=lambda: ["20261005-000000F"])
    watch_schedule_ok: bool = True
    calls: list[list[str]] = field(default_factory=list)
    seed_inputs: list[dict[str, Any]] = field(default_factory=list)

    def ok(self, stdout: str = "") -> Any:
        return bfx.CommandResult(0, stdout, "")

    def fail(self, stderr: str = "boom", code: int = 1) -> Any:
        return bfx.CommandResult(code, "", stderr)

    def names(self, prefix: str) -> list[list[str]]:
        return [call for call in self.calls if " ".join(call).startswith(prefix)]

    def index(self, prefix: str) -> int:
        for position, call in enumerate(self.calls):
            if " ".join(call).startswith(prefix):
                return position
        raise AssertionError(f"never called: {prefix}")

    def __call__(self, argv: Sequence[str], *, timeout: float, input_text: str | None = None,
                 env: Mapping[str, str] | None = None) -> Any:
        argv = list(argv)
        self.calls.append(argv)
        joined = " ".join(argv)
        if argv[:1] == ["systemd-run"]:
            return self.ok() if self.watch_schedule_ok else self.fail()
        if argv[:2] == ["systemctl", "is-active"]:
            unit = argv[2]
            return self.ok((self.weekly if unit == switch.WEEKLY_SERVICE
                            else self.timers.get(unit, "inactive")) + "\n")
        if argv[:2] == ["systemctl", "stop"]:
            self.timers[argv[2]] = "inactive"
            return self.ok()
        if argv[:2] == ["systemctl", "start"]:
            self.timers[argv[2]] = "active"
            return self.ok()
        if argv[:3] == ["docker", "ps", "--all"]:
            return self.ok("")  # the removed seed container is gone
        if argv[:2] == ["docker", "ps"]:
            return self.ok("\n".join([n for n, up in self.running.items() if up]
                                     + self.extra_containers) + "\n")
        if argv[:2] == ["docker", "inspect"]:
            records = [{"Name": "/" + name, "Config": {"Image": self.images.get(name, "")},
                        "State": {"Running": self.running.get(name, False)},
                        "NetworkSettings": {"Networks": {"bfx_default": {
                            "IPAddress": "" if self.no_ip else f"172.18.0.{index + 2}"}}}}
                       for index, name in enumerate(argv[4:])]
            return self.ok(json.dumps(records))
        if argv[:2] in (["docker", "stop"], ["docker", "start"]):
            self.running[argv[2]] = argv[1] == "start"
            return self.ok()
        if argv[:2] == ["docker", "rm"]:
            return self.ok()
        if argv[:2] == ["docker", "run"] and "/app/.venv/bin/alembic" in argv:
            return self.ok((self.current if argv[-1] == "current" else self.heads) + " (head)\n")
        if argv[:2] == ["docker", "run"] and switch.SEED_MODULE in argv:
            return self.seed(argv)
        if argv[:2] == ["docker", "run"] and "--volumes-from" in argv:
            assert not self.running["bfx-postgres"], "restore while postgres runs"
            if self.restore_ok:
                self.db.epoch, self.db.seed_observations, self.db.actor = "legacy", 0, "x"
                return self.ok()
            return self.fail()
        if argv[:1] == ["runuser"] and argv[-3].endswith("backup.sh"):
            if self.break_db_at == "backup":
                self.db.unreadable = True
            if self.backup_fails:
                return self.fail()
            self.labels.append(f"20261005-{len(self.labels):06d}D")
            return self.ok()
        if "pgbackrest" in argv and argv[-1] == "info":
            return self.ok(json.dumps([{"backup": [{"label": label} for label in self.labels]}]))
        if argv[:2] == ["docker", "exec"] and "pg_isready" in argv:
            return self.ok() if self.running["bfx-postgres"] else self.fail()
        if argv[:2] == ["docker", "exec"] and argv[-1].endswith("/ready"):
            return self.ok() if self.ready and self.running.get("bfx-webapi") else self.fail()
        if joined.startswith("/usr/local/sbin/bfx-deploy --recreate"):
            if not self.recreate_ok:
                self.db.writes.update(self.recreate_failure_writes)
                self.ledger.add("failed", DIGEST)
                self.running["bfx-bot"] = False
                return self.fail()
            self.ledger.add("deployed", DIGEST)
            self.running.update({"bfx-bot": True, "bfx-webapi": True})
            if self.recreate_writes:
                self.db.writes["observations"], self.db.basis = 1, True
            return self.ok()
        raise AssertionError(f"unexpected command: {argv}")

    def seed(self, argv: list[str]) -> Any:
        mount = next(argv[i + 1] for i, a in enumerate(argv) if a == "--volume")
        directory = Path(mount.split(":")[0])
        self.seed_inputs.append({
            "dsn": (directory / "dsn").read_text().strip(),
            "manifest": json.loads((directory / "manifest.json").read_text()),
            "argv": argv,
        })
        if "--check" in argv:
            reason = self.checks.pop(0) if self.checks else None
            return bfx.CommandResult(3 if reason else 0, seed_lines("check", reason=reason), "")
        run_id = self.seed_inputs[-1]["manifest"]["run_id"]
        outcome = self.switches.pop(0) if self.switches else None
        if outcome == "late":  # signalled mid-seed; its transaction commits afterwards
            self.db.late_commit = run_id
            raise switch.Terminated("signal_15")
        if isinstance(outcome, Exception):
            self.db.commit_seed(run_id)  # committed, then the container was lost
            if self.break_db_at == "seed":
                self.db.unreadable = True
            raise outcome
        if outcome is None:
            self.db.commit_seed(run_id)
            return self.ok(seed_lines("switch", share=self.share))
        return bfx.CommandResult(3, seed_lines("switch", reason=outcome), "")


@dataclass
class Env:
    host: FakeHost
    db: FakeDb
    ledger: FakeLedger
    clock: FakeClock
    notes: list[tuple[str, str]]
    settings: Any
    tmp: Path

    def switcher(self, run_id: str = "switch-test", **kwargs: Any) -> Any:
        options: dict[str, Any] = {"chown": lambda path, uid, gid: None, **kwargs}
        return switch.Switcher(
            self.settings, runner=self.host, db=self.db, ledger=self.ledger,
            notify=lambda level, text: self.notes.append((level, text)), clock=self.clock,
            sleep=self.clock.sleep, secret_check=lambda path: None, run_id=run_id, **options)

    def levels(self) -> list[str]:
        return [level for level, _ in self.notes]

    def evidence(self, name: str = "evidence.json", run_id: str = "switch-test") -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(
            (self.settings.state_dir / run_id / name).read_text())
        return loaded


@pytest.fixture
def env(tmp_path: Path) -> Env:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "bot.env").write_text(f"BFX_EXCHANGE_ACCOUNT_ID={ACCOUNT}\nOTHER=x\n")
    (runtime / "migrate.env").write_text(
        f"DATABASE_URL=postgresql://bfx:{DSN_PASSWORD}@bfx-postgres/bfx\n")
    soak = tmp_path / "soak-result.json"
    soak.write_text(json.dumps({"verdict": "PASS", "image_digest": DIGEST,
                                "last_service_version": "b" * 40,
                                "window": {"since_ms": 1, "until_ms": 2},
                                "report_sha256": "f" * 64}))
    clock = FakeClock()
    db = FakeDb(clock)
    ledger = FakeLedger()
    host = FakeHost(clock, db, ledger)
    db.host = host
    settings = switch.Settings(
        digest=DIGEST, runtime_dir=runtime, state_dir=tmp_path / "state",
        lock_file=tmp_path / "switch.lock", deploy_lock_file=tmp_path / "deploy.lock",
        dr_current=tmp_path / "dr-current", soak_result=soak)
    return Env(host, db, ledger, clock, [], settings, tmp_path)


def stops(host: FakeHost) -> list[list[str]]:
    return [c for c in host.calls if c[:2] in (["docker", "stop"], ["systemctl", "stop"])]


def no_secret_leaked(env: Env) -> None:
    for _, text in env.notes:
        assert DSN_PASSWORD not in text
    for path in (env.settings.state_dir / "switch-test").rglob("*"):
        if path.is_file():
            assert DSN_PASSWORD not in path.read_text(), path
    assert not (env.settings.state_dir / "switch-test" / "seed-input").exists()


# --------------------------------------------------------------------------- success


def test_the_switch_succeeds_end_to_end(env: Env) -> None:
    host = env.host
    assert env.switcher().run() == switch.EXIT_OK
    # Timers first (and bfx-deploy's among them), then the web API, then the bot.
    deploy_timer = host.index("systemctl stop bfx-deploy.timer")
    assert deploy_timer < host.index("docker stop bfx-webapi") < host.index("docker stop bfx-bot")
    assert host.index("systemctl stop bfx-weekly-report.timer") < host.index("docker stop bfx-webapi")
    assert host.index("docker stop bfx-bot") < host.index("runuser") < host.index(
        "docker run --rm --name bfx-ledger-switch-switch") < host.index(
        "/usr/local/sbin/bfx-deploy --recreate")
    assert host.timers == INITIAL_TIMERS  # inactive stays inactive, the rest are back
    assert not host.names("systemctl stop bfx-pgbackrest-backup.timer")
    assert env.db.epoch == "ledger"
    # The seed ran as the owner DSN of migrate.env (port made explicit), via files only.
    check, seeded = host.seed_inputs[0], host.seed_inputs[-1]
    assert seeded["dsn"] == f"postgresql://bfx:{DSN_PASSWORD}@bfx-postgres:5432/bfx"
    assert seeded["manifest"] == {"mode": "seed", "run_id": "switch-test-a1",
                                  "host": "bfx-postgres", "port": 5432, "database": "bfx",
                                  "user": "bfx", "realm": "prod",
                                  "scopes": [f"{ACCOUNT}:prod"]}
    assert "--authorize-seed" in seeded["argv"] and "--authorize-seed" not in check["argv"]
    assert all(DSN_PASSWORD not in " ".join(call) for call in host.calls)
    evidence = env.evidence()
    assert evidence["outcome"] == "switched"
    assert [s["step"] for s in evidence["steps"]] == [
        "preflight#1", "stop_timers", "deploy_lock", "record_resting", "stop_webapi",
        "wait_legacy_snapshot", "stop_bot", "backup", "seed_switch", "recreate",
        "wait_ledger_live", "nav_continuity", "schedule_watch"]
    assert all(s["result"] == "ok" and s["end_ms"] >= s["start_ms"] for s in evidence["steps"])
    assert evidence["resting"] == {"snapshot_event_seq": 41, "offers": 3,
                                   "symbols": {"fUST": {"offered": "1200", "foreign": "75"}},
                                   "total_capital": {"fUST": "5000"}}
    (watch,) = host.names("systemd-run")
    assert "--on-active=24h" in watch and watch[-3:] == ["watch", "--run-id", "switch-test"]
    assert evidence["soak_result"]["report_sha256"] == "f" * 64
    assert evidence["backup_label"] == host.labels[-1]
    assert env.levels() == ["info"]
    no_secret_leaked(env)


def test_a_large_recent_fill_share_notifies_and_continues(env: Env) -> None:
    env.host.share = "0.250000"
    assert env.switcher().run() == switch.EXIT_OK
    assert env.levels() == ["warning", "info"]
    assert "F7" in env.notes[0][1]


def test_a_transient_check_refusal_is_rechecked(env: Env) -> None:
    env.host.checks = ["legacy_query_pending"]
    assert env.switcher().run() == switch.EXIT_OK
    assert len([i for i in env.host.seed_inputs if "--check" in i["argv"]]) == 2


# --------------------------------------------------------------------------- P-checks


def _soaked_other_digest(env: Env) -> None:
    env.ledger.add("deployed", OTHER)


def _last_attempt_failed(env: Env) -> None:
    env.ledger.add("failed", DIGEST)


def _container_digest(env: Env) -> None:
    env.host.images["bfx-webapi"] = f"{REPO}@{OTHER}"


def _bot_not_running(env: Env) -> None:
    env.host.running["bfx-bot"] = False


def _schema_behind(env: Env) -> None:
    env.host.current = "b5c6d7e8f9a0"


def _weekly_running(env: Env) -> None:
    env.host.weekly = "active"


def _sim_running(env: Env) -> None:
    env.host.extra_containers = ["bfx-sim"]


def _foreign_session(env: Env) -> None:
    env.db.foreign_sessions = 1


def _check_refused(env: Env) -> None:
    env.host.checks = ["credit_opening_unkeyable"]


def _check_transient_forever(env: Env) -> None:
    env.host.checks = ["attempt_outcome_missing"] * 3


def _soak_result_missing(env: Env) -> None:
    env.settings.soak_result.unlink()


def _soak_result_fail(env: Env) -> None:
    _rewrite_soak(env, verdict="FAIL")


def _soak_result_other_digest(env: Env) -> None:
    _rewrite_soak(env, image_digest=OTHER)


def _soak_result_other_revision(env: Env) -> None:
    _rewrite_soak(env, last_service_version="c" * 40)


def _rewrite_soak(env: Env, **changes: str) -> None:
    path = env.settings.soak_result
    path.write_text(json.dumps({**json.loads(path.read_text()), **changes}))


def _bad_digest_argument(env: Env) -> None:
    env.settings = switch.Settings(**{**{f: getattr(env.settings, f) for f in
                                         env.settings.__dataclass_fields__}, "digest": "latest"})


@pytest.mark.parametrize(("break_it", "code"), [
    (_soaked_other_digest, "running_digest_is_not_the_soaked_digest"),
    (_last_attempt_failed, "last_deploy_not_successful"),
    (_container_digest, "running_digest_mismatch:bfx-webapi"),
    (_bot_not_running, "legacy_not_running:bfx-bot"),
    (_schema_behind, "schema_not_at_head"),
    (_weekly_running, "weekly_report_running"),
    (_sim_running, "simulation_running"),
    (_foreign_session, "runtime_session_present"),
    (_check_refused, "seed_check_refused:credit_opening_unkeyable"),
    (_check_transient_forever, "seed_check_refused:attempt_outcome_missing"),
    (_bad_digest_argument, "digest_invalid"),
    (_soak_result_missing, "soak_result_missing"),
    (_soak_result_fail, "soak_result_not_pass"),
    (_soak_result_other_digest, "soak_result_digest_mismatch"),
    (_soak_result_other_revision, "soak_result_revision_mismatch"),
], ids=lambda value: value if isinstance(value, str) else value.__name__.strip("_"))
def test_every_p_check_failure_stops_nothing(env: Env, break_it: Callable[[Env], None],
                                             code: str) -> None:
    break_it(env)
    assert env.switcher().run() == switch.EXIT_PRECHECK
    assert stops(env.host) == []
    assert env.host.timers == INITIAL_TIMERS
    assert env.evidence()["outcome"] == f"precheck_failed:{code}"
    assert env.levels() == ["warning"]
    assert not env.host.names("/usr/local/sbin/bfx-deploy")


def _waived(env: Env, reason: str = "Will: single user, skip the soak") -> None:
    env.settings = switch.Settings(**{**{f: getattr(env.settings, f) for f in
                                         env.settings.__dataclass_fields__},
                                      "soak_waiver": reason})


def test_a_waiver_replaces_the_soak_result_and_is_recorded(env: Env) -> None:
    env.settings.soak_result.unlink()
    _waived(env)
    assert env.switcher().run() == switch.EXIT_OK
    assert env.evidence()["soak_result"] == {"waived": "Will: single user, skip the soak"}


def test_a_waiver_skips_only_the_soak_check(env: Env) -> None:
    _waived(env)
    _soaked_other_digest(env)
    assert env.switcher().run() == switch.EXIT_PRECHECK
    assert env.evidence()["outcome"] == "precheck_failed:running_digest_is_not_the_soaked_digest"


def test_the_cli_waiver_needs_a_reason_and_excludes_a_result_file() -> None:
    parser = switch._parser()
    args = parser.parse_args(["run", "--digest", DIGEST, "--waive-soak", " skip "])
    assert args.waive_soak == "skip"
    assert parser.parse_args(["preflight", "--digest", DIGEST]).waive_soak is None
    for argv in (["run", "--digest", DIGEST, "--waive-soak", "  "],
                 ["run", "--digest", DIGEST, "--waive-soak", "x", "--soak-result", "/r.json"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_the_session_check_excludes_only_the_legacy_containers(env: Env) -> None:
    assert env.switcher().preflight() == switch.EXIT_OK
    assert env.db.seen_addresses == [("172.18.0.2", "172.18.0.3")]
    assert stops(env.host) == []


# --------------------------------------------------------------------------- R1


def test_r1_snapshot_wait_times_out_and_legacy_restarts(env: Env) -> None:
    env.db.produce_snapshots = False
    assert env.switcher().run() == switch.EXIT_R1
    host = env.host
    assert host.index("docker start bfx-webapi") > host.index("docker stop bfx-webapi")
    assert host.running["bfx-bot"] and host.running["bfx-webapi"]
    assert not host.names("runuser") and not host.seed_inputs[1:]
    assert host.timers == INITIAL_TIMERS
    assert env.evidence()["outcome"].startswith("r1:legacy_snapshot_timeout")
    assert env.levels() == ["warning"]


def test_a_sigterm_mid_halt_takes_the_failure_branch(env: Env) -> None:
    """systemd's timeout or `systemctl stop` during the snapshot wait: R1, timers restored."""
    env.db.produce_snapshots = False

    def terminated(seconds: float) -> None:
        raise switch.Terminated("signal_15")

    switcher = switch.Switcher(
        env.settings, runner=env.host, db=env.db, ledger=env.ledger,
        notify=lambda level, text: env.notes.append((level, text)), clock=env.clock,
        sleep=terminated, secret_check=lambda path: None, chown=lambda path, uid, gid: None,
        run_id="switch-test")
    assert switcher.run() == switch.EXIT_R1
    assert env.host.running["bfx-bot"] and env.host.running["bfx-webapi"]
    assert env.host.timers == INITIAL_TIMERS
    assert env.evidence()["outcome"] == "r1:terminated"


def test_settings_share_bfx_deploys_lock_and_names() -> None:
    deploy, ours = bfx.Settings(), switch.Settings()
    assert ours.deploy_lock_file == deploy.lock_file
    assert (ours.runtime_dir, ours.postgres_container, ours.network, ours.backend_repository) == (
        deploy.runtime_dir, deploy.postgres_container, deploy.network, deploy.backend_repository)


def test_r1_backup_failure_restarts_legacy(env: Env) -> None:
    env.host.backup_fails = True
    assert env.switcher().run() == switch.EXIT_R1
    assert env.host.running["bfx-bot"] and env.host.running["bfx-webapi"]
    assert env.db.epoch == "legacy" and env.host.timers == INITIAL_TIMERS


def test_r1_seed_refusal_restarts_legacy_without_retry(env: Env) -> None:
    env.host.switches = ["claim_offer_terms_mismatch"]
    assert env.switcher().run() == switch.EXIT_R1
    assert len([i for i in env.host.seed_inputs if "--switch" in i["argv"]]) == 1
    assert env.host.running["bfx-bot"] and env.host.timers == INITIAL_TIMERS


def test_an_f6_refusal_retries_the_whole_run_once(env: Env) -> None:
    env.host.switches = ["attempt_outcome_missing"]
    assert env.switcher().run() == switch.EXIT_OK
    steps = [s["step"] for s in env.evidence()["steps"]]
    assert steps.count("preflight#1") == steps.count("preflight#2") == 1
    assert steps.index("r1_start_legacy") < steps.index("wait_legacy_recovery") < steps.index(
        "preflight#2")
    assert len([i for i in env.host.seed_inputs if "--switch" in i["argv"]]) == 2
    assert env.db.epoch == "ledger" and env.host.timers == INITIAL_TIMERS


def test_the_retry_happens_at_most_once(env: Env) -> None:
    env.host.switches = ["legacy_query_pending", "legacy_query_pending"]
    assert env.switcher().run() == switch.EXIT_R1
    assert len([i for i in env.host.seed_inputs if "--switch" in i["argv"]]) == 2
    assert env.host.running["bfx-bot"] and env.host.timers == INITIAL_TIMERS


# --------------------------------------------------------------------------- R2 / R3


def test_r2_keeps_the_bot_stopped_and_names_the_restore(env: Env) -> None:
    env.host.recreate_ok = False
    assert env.switcher().run() == switch.EXIT_R2
    host = env.host
    seed_at = host.index("docker run --rm --name bfx-ledger-switch-switch")
    assert not [c for c in host.calls[seed_at:] if c[:3] == ["docker", "start", "bfx-bot"]]
    assert not host.running["bfx-bot"]
    assert host.timers == INITIAL_TIMERS
    level, text = env.notes[-1]
    assert level == "critical" and "R2" in text and "restore-halt-backup --run-id switch-test" in text
    assert "resting at halt start" in text
    assert env.evidence()["outcome"] == "r2:recreate_failed"


def test_r2_when_the_seed_committed_but_its_container_was_lost(env: Env) -> None:
    env.host.switches = [bfx.CommandError("timeout:docker run", timed_out=True)]
    assert env.switcher().run() == switch.EXIT_R2
    assert not env.host.running["bfx-bot"]
    assert not env.host.names("/usr/local/sbin/bfx-deploy")
    assert env.host.names("docker rm --force")


def test_an_unreadable_database_after_the_seed_started_is_treated_as_r2(env: Env) -> None:
    env.host.switches = [bfx.CommandError("timeout:docker run", timed_out=True)]
    env.host.break_db_at = "seed"
    assert env.switcher().run() == switch.EXIT_R2
    assert not env.host.running["bfx-bot"]


def test_a_failure_before_the_seed_with_an_unreadable_database_is_r1(env: Env) -> None:
    """R1-5: the seed never ran, so nothing can have been written: legacy restarts."""
    env.host.backup_fails = True
    env.host.break_db_at = "backup"
    assert env.switcher().run() == switch.EXIT_R1
    assert env.host.running["bfx-bot"] and env.host.running["bfx-webapi"]
    assert env.host.timers == INITIAL_TIMERS


def test_r3_after_the_first_runtime_write_is_forward_fix_only(env: Env) -> None:
    env.db.unexplained = ("fUST",)
    assert env.switcher().run() == switch.EXIT_R3
    host = env.host
    recreate = host.index("/usr/local/sbin/bfx-deploy --recreate")
    assert not [c for c in host.calls[recreate:] if c[:2] == ["docker", "stop"]]
    level, text = env.notes[-1]
    assert level == "critical" and "R3" in text and "Forward-fix only" in text
    assert "unexplained_lending:fUST" in env.evidence()["outcome"]
    assert host.timers == INITIAL_TIMERS


def test_a_recreate_that_wrote_nothing_and_never_got_ready_is_r2(env: Env) -> None:
    env.host.recreate_writes = False
    env.host.ready = False
    assert env.switcher().run() == switch.EXIT_R2


# --------------------------------------------------------------------------- restore


def _committed_run(env: Env) -> None:
    env.host.recreate_ok = False
    assert env.switcher().run() == switch.EXIT_R2
    env.notes.clear()
    env.host.calls.clear()


def test_restore_refuses_after_a_runtime_ledger_observation(env: Env) -> None:
    _committed_run(env)
    env.db.writes["observations"] = 1
    assert env.switcher().restore_halt_backup() == switch.EXIT_RESTORE_REFUSED
    assert stops(env.host) == []
    assert not env.host.names("docker run")
    assert env.evidence("restore.json")["outcome"] == "refused:runtime_ledger_write_exists"


def test_restore_refuses_when_the_seed_never_committed(env: Env) -> None:
    env.host.backup_fails = True
    assert env.switcher().run() == switch.EXIT_R1
    (env.settings.state_dir / "switch-test" / "evidence.json").write_text(json.dumps(
        {"backup_label": "x", "account": ACCOUNT}))
    assert env.switcher().restore_halt_backup() == switch.EXIT_RESTORE_REFUSED


def test_restore_puts_the_halt_backup_back_and_backs_up_at_once(env: Env) -> None:
    _committed_run(env)
    halt_label = env.evidence()["backup_label"]
    assert env.switcher().restore_halt_backup() == switch.EXIT_OK
    host = env.host
    restore = next(c for c in host.calls if "--volumes-from" in c)
    assert f"--set={halt_label}" in restore and "--delta" in restore
    assert host.index("systemctl stop bfx-deploy.timer") < host.index("docker stop bfx-bot") \
        < host.index("docker stop bfx-postgres") < host.index("docker run --rm --name "
                                                              "bfx-ledger-switch-restore") \
        < host.index("docker start bfx-postgres")
    backup = [c for c in host.calls if c[:1] == ["runuser"]]
    assert backup and backup[-1][-2:] == ["--type", "full"]
    assert host.index("docker start bfx-postgres") < host.calls.index(backup[-1]) \
        < host.index("docker start bfx-bot")
    assert env.db.epoch == "legacy" and host.running["bfx-bot"]
    assert host.timers == INITIAL_TIMERS
    assert env.evidence("restore.json")["outcome"] == "restored"
    assert env.levels() == ["info"]


def test_a_failed_restore_leaves_postgres_alone_and_alerts(env: Env) -> None:
    _committed_run(env)
    env.host.restore_ok = False
    assert env.switcher().restore_halt_backup() == switch.EXIT_RESTORE_FAILED
    assert not env.host.names("docker start")
    assert env.levels() == ["critical"] and env.host.timers == INITIAL_TIMERS


# --------------------------------------------------------------------------- pieces


def test_seed_dsn_never_changes_an_explicit_port_and_refuses_query_strings() -> None:
    dsn, fields = switch.seed_dsn("DATABASE_URL=postgresql://bfx:p@db:6543/bfx\n")
    assert dsn == "postgresql://bfx:p@db:6543/bfx" and fields["port"] == 6543
    with pytest.raises(switch.SwitchError, match="migrate_dsn_unusable"):
        switch.seed_dsn("DATABASE_URL=postgresql://bfx:p@db:5432/bfx?sslmode=require\n")
    with pytest.raises(switch.SwitchError, match="migrate_dsn_unusable"):
        switch.seed_dsn("OTHER=1\n")


def test_a_second_run_cannot_start_while_one_holds_the_lock(env: Env) -> None:
    import fcntl
    env.settings.lock_file.parent.mkdir(parents=True, exist_ok=True)
    with env.settings.lock_file.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert env.switcher().run() == switch.EXIT_USAGE
    assert env.host.calls == []


def test_a_running_deploy_is_waited_for_then_r1(env: Env) -> None:
    import fcntl
    env.settings.deploy_lock_file.parent.mkdir(parents=True, exist_ok=True)
    with env.settings.deploy_lock_file.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert env.switcher().run() == switch.EXIT_R1
    assert not env.host.names("docker stop")
    assert env.host.timers == INITIAL_TIMERS
    assert env.evidence()["outcome"].startswith("r1:deploy_lock_timeout")


# --------------------------------------------------------------------------- post-switch


def test_a_nav_breach_notifies_and_the_switch_still_succeeds(env: Env) -> None:
    env.db.ledger_totals = {"fUST": "5100"}  # +2 %: beyond what interest during a halt explains
    assert env.switcher().run() == switch.EXIT_OK
    assert env.levels() == ["warning", "info"]
    assert "NAV continuity" in env.notes[0][1] and "fUST" in env.notes[0][1]
    (nav,) = [s for s in env.evidence()["steps"] if s["step"] == "nav_continuity"]
    assert (nav["result"], nav["breaches"]) == ("breach", ["fUST"])


def test_nav_breaches_use_the_relative_tolerance_per_symbol() -> None:
    assert switch.nav_breaches({"fUST": "1000"}, {"fUST": "1004.9"}) == []
    assert switch.nav_breaches({"fUST": "1000"}, {"fUST": "994"}) == ["fUST"]
    assert switch.nav_breaches({"fUST": "1000"}, {"fUST": "1000", "fUSD": "1"}) == ["fUSD"]
    assert switch.nav_breaches({"fUSD": "0"}, {}) == []


def test_a_failed_watch_schedule_warns_and_the_switch_still_succeeds(env: Env) -> None:
    env.host.watch_schedule_ok = False
    assert env.switcher().run() == switch.EXIT_OK
    assert env.levels() == ["warning", "info"] and "schedule_watch" in env.notes[0][1]


def test_the_watch_notifies_only_when_nothing_traded(env: Env) -> None:
    assert env.switcher().run() == switch.EXIT_OK
    switched_at = env.evidence()["switched_at_ms"]
    env.notes.clear()
    assert env.switcher().watch() == switch.EXIT_OK
    assert env.db.activity_asked == [switched_at]
    assert env.levels() == ["warning"] and "no acknowledged submit" in env.notes[0][1]
    assert env.evidence("watch.json")["acked_submits"] == 0
    env.notes.clear()
    env.db.activity = (0, 1)  # one reprice
    assert env.switcher().watch() == switch.EXIT_OK
    assert env.notes == []


# --------------------------------------------------------------------------- review R1


def test_a_sigterm_during_the_seed_waits_for_its_late_commit_then_r2(env: Env) -> None:
    """R1-1: the seed container commits after the signal; the tool removes it, waits until no
    owner transaction remains, and only then reads the epoch: R2, never R1."""
    env.host.switches = ["late"]
    assert env.switcher().run() == switch.EXIT_R2
    host = env.host
    seed_at = host.index("docker run --rm --name bfx-ledger-switch-switch")
    assert host.index("docker rm --force bfx-ledger-switch-switch") > seed_at
    assert env.db.owner_polls >= 2 and env.db.epoch == "ledger"
    assert not [c for c in host.calls[seed_at:] if c[:3] == ["docker", "start", "bfx-bot"]]
    assert not host.running["bfx-bot"] and host.timers == INITIAL_TIMERS
    assert env.evidence()["outcome"].startswith("r2:")


def test_a_seed_that_never_settles_is_r2(env: Env) -> None:
    env.host.switches = ["late"]
    env.db.late_commit = None

    def stuck(user: str) -> int:
        env.db.owner_polls += 1
        return 1

    env.db.owner_transactions = stuck  # type: ignore[method-assign]
    assert env.switcher().run() == switch.EXIT_R2
    assert env.db.epoch == "legacy"  # unknown is never taken as "nothing was written"
    assert not env.host.running["bfx-bot"]


WRITE_KINDS = ("observations", "queries", "attempts", "resolutions", "quarantines", "clock")


@pytest.mark.parametrize("kind", WRITE_KINDS)
def test_every_runtime_write_kind_turns_r2_into_r3(env: Env, kind: str) -> None:
    """R1-2: any runtime ledger row, not only a basis-backed observation."""
    env.host.recreate_ok = False
    env.host.recreate_failure_writes = {kind: 1}
    assert env.switcher().run() == switch.EXIT_R3
    assert env.evidence()["runtime_writes"] == {kind: 1}


@pytest.mark.parametrize("kind", WRITE_KINDS)
def test_every_runtime_write_kind_blocks_the_restore(env: Env, kind: str) -> None:
    _committed_run(env)
    env.db.writes[kind] = 1
    assert env.switcher().restore_halt_backup() == switch.EXIT_RESTORE_REFUSED
    assert stops(env.host) == [] and not env.host.names("docker run")


def test_restore_refuses_another_runs_backup(env: Env) -> None:
    """R1-3: an earlier run that ended in R1 left a backup label but no committed seed."""
    env.host.switches = ["claim_offer_terms_mismatch"]
    assert env.switcher(run_id="switch-old").run() == switch.EXIT_R1
    assert env.evidence(run_id="switch-old")["backup_label"]
    _committed_run(env)  # switch-test commits and ends in R2
    assert env.switcher(run_id="switch-old").restore_halt_backup() == \
        switch.EXIT_RESTORE_REFUSED
    assert stops(env.host) == [] and not env.host.names("docker run")


def test_restore_refuses_when_the_epoch_is_not_this_runs(env: Env) -> None:
    _committed_run(env)
    env.db.actor = "ledger_seed:switch-other-a1"
    assert env.switcher().restore_halt_backup() == switch.EXIT_RESTORE_REFUSED
    assert env.evidence("restore.json")["outcome"] == "refused:epoch_not_from_this_run"
    assert stops(env.host) == []


def _crashed_after_commit(env: Env) -> None:
    """switch-test committed its seed and died before go-live: bot stopped, timers stopped."""
    _committed_run(env)
    for timer, state in INITIAL_TIMERS.items():
        if state == "active":
            env.host.timers[timer] = "inactive"


def test_a_rerun_after_a_commit_reports_r2_of_that_run_and_restores_its_timers(env: Env) -> None:
    """R1-4: never "nothing was stopped, legacy keeps running"."""
    _crashed_after_commit(env)
    assert env.switcher(run_id="switch-rerun").run() == switch.EXIT_R2
    assert env.host.timers == INITIAL_TIMERS
    assert not [c for c in env.host.calls if c[:2] in (["docker", "stop"], ["docker", "start"])]
    level, text = env.notes[-1]
    assert level == "critical" and "restore-halt-backup --run-id switch-test" in text
    assert "legacy keeps running" not in text
    assert env.evidence(run_id="switch-rerun")["outcome"] == "already_switched:switch-test"


def test_a_rerun_after_a_runtime_write_reports_r3(env: Env) -> None:
    _crashed_after_commit(env)
    env.db.writes["attempts"] = 1
    assert env.switcher(run_id="switch-rerun").run() == switch.EXIT_R3
    assert "R3" in env.notes[-1][1]


def test_preflight_after_a_commit_reports_without_touching_anything(env: Env) -> None:
    _crashed_after_commit(env)
    assert env.switcher(run_id="switch-pre").preflight() == switch.EXIT_R2
    assert env.host.calls == [] or not [c for c in env.host.calls if c[:2] in (
        ["systemctl", "start"], ["systemctl", "stop"], ["docker", "stop"], ["docker", "start"])]


def test_the_dsn_never_stays_on_disk_when_writing_the_inputs_fails(env: Env) -> None:
    """R1-6: chown fails after the DSN file was written."""
    def failing_chown(path: Path, uid: int, gid: int) -> None:
        raise PermissionError("chown")

    assert env.switcher(chown=failing_chown).run() == switch.EXIT_PRECHECK
    assert not (env.settings.state_dir / "switch-test" / "seed-input").exists()
    assert stops(env.host) == []
    no_secret_leaked(env)


def test_no_legacy_container_address_fails_the_session_check_closed(env: Env) -> None:
    """R1-7: an empty exclusion set would let every TCP session through unseen."""
    env.host.no_ip = True
    assert env.switcher().run() == switch.EXIT_PRECHECK
    assert env.evidence()["outcome"] == "precheck_failed:legacy_container_addresses_unknown"
    assert env.db.seen_addresses == []


# --------------------------------------------------------------------------- review R2


def test_a_seed_lost_after_its_commit_is_r2_and_its_restore_is_allowed(env: Env) -> None:
    """R2-1: the seed container timed out after committing; the R2 notice names
    restore-halt-backup, and that restore runs (the epoch actor ties it to this run)."""
    env.host.switches = [bfx.CommandError("timeout:docker run", timed_out=True)]
    assert env.switcher().run() == switch.EXIT_R2
    assert "restore-halt-backup --run-id switch-test" in env.notes[-1][1]
    assert env.evidence()["seed_committed"] is True
    env.notes.clear()
    assert env.switcher().restore_halt_backup() == switch.EXIT_OK
    assert env.db.epoch == "legacy" and env.levels() == ["info"]


def test_the_settle_wait_watches_the_seed_dsn_login(env: Env) -> None:
    """R2-2: the seed connects as migrate.env's user, not the psql owner of the host reads."""
    (env.settings.runtime_dir / "migrate.env").write_text(
        f"DATABASE_URL=postgresql://bfx_owner:{DSN_PASSWORD}@bfx-postgres:5432/bfx\n")
    assert env.settings.db_user == "bfx"
    env.host.switches = ["late"]
    assert env.switcher().run() == switch.EXIT_R2
    assert env.db.owner_users and set(env.db.owner_users) == {"bfx_owner"}


def test_a_restore_after_a_crash_before_the_evidence_flag_is_allowed(env: Env) -> None:
    """R2-1: the tool died between the commit and writing ``seed_committed`` (no flag in the
    evidence); the epoch actor alone decides that this run's halt backup may be restored."""
    _committed_run(env)
    path = env.settings.state_dir / "switch-test" / "evidence.json"
    evidence = json.loads(path.read_text())
    evidence.pop("seed_committed", None)
    path.write_text(json.dumps(evidence))
    assert env.switcher().restore_halt_backup() == switch.EXIT_OK
    assert env.db.epoch == "legacy"
