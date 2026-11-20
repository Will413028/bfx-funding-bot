"""The weekly-report job as release-managed host tooling.

deploy/vm/ops/docker-compose.weekly-report.yml holds the steps (checked in CI by
compose_policy.py --kind weekly-report on Compose's own rendering), and
deploy/vm/ops/bfx_weekly_report.py resolves what it runs: the ledger's deployed
backend digest, three env values (two from research.env, the research role's file),
and the compose file of its own release.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PATH = ROOT / "deploy/vm/ops/docker-compose.weekly-report.yml"
COMPOSE_TEXT = COMPOSE_PATH.read_text(encoding="utf-8")
COMPOSE = yaml.safe_load(COMPOSE_TEXT)
BACKEND = "ghcr.io/will413028/bfx-funding-bot-backend"
DIGEST = "sha256:" + "3" * 64
OLD_DIGEST = "sha256:" + "1" * 64
REV = "b" * 40
OLD_REV = "a" * 40
FAKE_ENV = {
    "BFX_BACKEND_IMAGE": f"{BACKEND}@{DIGEST}",
    "BFX_BACKEND_DIGEST": DIGEST,
    "BFX_SOURCE_REVISION": REV,
    "DATABASE_URL": "postgresql://example@bfx-postgres:5432/example",
    "BFX_EXCHANGE_ACCOUNT_ID": "00000000-0000-4000-8000-000000000000",
}

# The chain exactly as docker-compose.bot.yml defined it before the move
# (including report_interest), plus the delivery metrics: moving the job must not change a step.
EXPECTED_STEPS = [
    'timeout 600 python -m scripts.ingest_funding_stats --symbols fUST,fUSD || echo "WARN ingest_funding_stats failed/timed out (frr arm may be stale — non-blocking)"',
    'python -m scripts.run_weekly_attribution --out "/reports/$$(date +%F)-attribution-reconciliation.md"',
    'python -m scripts.report_interest --out "/reports/$$(date +%F)-realized-interest.md" || echo "WARN report_interest failed (non-blocking)"',
    'python -m scripts.run_g3_live_validation --out "/reports/$$(date +%F)-g3-live-validation.md"',
    'python -m scripts.report_delivery_metrics --out "/reports/$$(date +%F)-delivery-metrics.md" || echo "WARN report_delivery_metrics failed (non-blocking)"',
    "# ── research re-validation (Proposal E, 2026-09-22): runs AFTER the operational",
    "# report, every step fail-soft, nothing here promotes anything. Budget: see",
    "# deploy/vm/systemd/bfx-weekly-report.service TimeoutStartSec.",
    'timeout 900 python -m scripts.ingest_perp_funding --skip-binance || echo "WARN ingest_perp_funding failed/timed out (non-blocking)"',
    'timeout 1200 python -m scripts.ingest_liquidations || echo "WARN ingest_liquidations failed/timed out (non-blocking)"',
    'timeout 600 python -m scripts.learn_book_fill_rate || echo "WARN learn_book_fill_rate failed/timed out (non-blocking)"',
    'timeout 600 python -m scripts.run_period_structure_backtest --fill-model book --output "/reports/$$(date +%F)-period-structure-book.md" || echo "WARN period-structure (book) failed (non-blocking)"',
    'timeout 600 python -m scripts.run_period_structure_backtest --output "/reports/$$(date +%F)-period-structure-linear.md" || echo "WARN period-structure (linear) failed (non-blocking)"',
    'timeout 600 python -m scripts.run_oos_profitability --output "/reports/$$(date +%F)-oos-profitability.md" || echo "WARN run_oos_profitability failed (non-blocking)"',
    'python -m scripts.diff_research_report --kind period-structure --glob "/reports/*-period-structure-book.json" --out "/reports/$$(date +%F)-weekly-research-book.md" || echo "WARN diff (book) failed (non-blocking)"',
    'python -m scripts.diff_research_report --kind oos --glob "/reports/*-oos-profitability.json" --out "/reports/$$(date +%F)-weekly-research-oos.md" || echo "WARN diff (oos) failed (non-blocking)"',
]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


policy = _load("vm_ops_compose_policy_for_weekly", ROOT / "deploy/vm/ops/compose_policy.py")
weekly = _load("vm_ops_weekly_report_under_test", ROOT / "deploy/vm/ops/bfx_weekly_report.py")
bfx = weekly.bfx_deploy

COMPOSE_CMD = os.environ.get("BFX_TEST_COMPOSE", "docker compose").split()


def _compose_available() -> bool:
    if shutil.which(COMPOSE_CMD[0]) is None:
        return False
    return subprocess.run([*COMPOSE_CMD, "version"], capture_output=True,
                          check=False).returncode == 0


needs_compose = pytest.mark.skipif(not _compose_available(), reason="docker compose CLI unavailable")


def _path_env() -> dict[str, str]:
    return {key: os.environ[key] for key in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT")
            if key in os.environ}


def _render(env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*COMPOSE_CMD, "-f", str(COMPOSE_PATH), "config", "--format", "json"],
                          capture_output=True, text=True, check=False, env={**_path_env(), **env})


@pytest.fixture(scope="module")
def rendered() -> dict[str, Any]:
    if not _compose_available():
        pytest.skip("docker compose CLI unavailable")
    result = _render(FAKE_ENV)
    assert result.returncode == 0, result.stderr
    return dict(json.loads(result.stdout))


# --------------------------------------------------------------------------- file shape


def test_steps_are_exactly_the_chain_the_vm_checkout_ran() -> None:
    command = COMPOSE["services"]["weekly-report"]["command"]
    assert command[:2] == ["sh", "-c"]
    assert command[2].splitlines() == EXPECTED_STEPS


def test_the_legacy_infrastructure_file_no_longer_defines_the_job() -> None:
    legacy = yaml.safe_load((ROOT / "docker-compose.bot.yml").read_text(encoding="utf-8"))
    assert "weekly-report" not in legacy["services"]
    assert "--profile ops" not in (ROOT / "deploy/vm/systemd/bfx-weekly-report.service").read_text()


def test_no_tag_no_env_file_and_the_runner_supplies_every_variable() -> None:
    service = COMPOSE["services"]["weekly-report"]
    assert service["image"].startswith("${") and "env_file" not in service
    assert re.search(r"bfx-bot:local|ghcr\.io/\S+:(main|sha-|latest)", COMPOSE_TEXT) is None
    required = set(re.findall(r"\$\{([A-Z_]+):\?", COMPOSE_TEXT))
    assert required == set(FAKE_ENV)
    assert set(weekly.SECRET_KEYS) | {
        "BFX_BACKEND_IMAGE", "BFX_BACKEND_DIGEST", "BFX_SOURCE_REVISION"} == required


# --------------------------------------------------------------------------- rendered policy


@needs_compose
def test_rendered_file_satisfies_the_weekly_policy(rendered: dict[str, Any]) -> None:
    assert policy.weekly_violations(rendered) == []


@needs_compose
def test_policy_cli_weekly_kind_is_what_ci_runs(rendered: dict[str, Any]) -> None:
    argv = [sys.executable, str(ROOT / "deploy/vm/ops/compose_policy.py"), "--kind", "weekly-report"]
    ok = subprocess.run(argv, input=json.dumps(rendered), capture_output=True, text=True, check=False)
    assert ok.returncode == 0, ok.stderr
    broken = copy.deepcopy(rendered)
    broken["services"]["weekly-report"]["env_file"] = [{"path": "/opt/bfx/runtime/bot.env"}]
    bad = subprocess.run(argv, input=json.dumps(broken), capture_output=True, text=True, check=False)
    assert bad.returncode == 1 and "weekly-report:no_env_file" in bad.stderr
    # The app policy still insists on its uninterpolated rendering.
    app = subprocess.run([sys.executable, str(ROOT / "deploy/vm/ops/compose_policy.py")],
                         input="{}", capture_output=True, text=True, check=False)
    assert app.returncode == 2 and "--uninterpolated" in app.stderr


REMOVALS: dict[str, Callable[[dict[str, Any]], None]] = {
    "image_digest_reference": lambda s: s.__setitem__("image", "bfx-bot:local"),
    "pull_policy_never": lambda s: s.pop("pull_policy"),
    "entrypoint_cleared": lambda s: s.pop("entrypoint"),
    "shell_command": lambda s: s.__setitem__("command", ["python", "-m", "x"]),
    "user": lambda s: s.pop("user"),
    "workdir": lambda s: s.pop("working_dir"),
    "read_only": lambda s: s.pop("read_only"),
    "cap_drop_all": lambda s: s.pop("cap_drop"),
    "no_cap_add": lambda s: s.__setitem__("cap_add", ["NET_ADMIN"]),
    "no_new_privileges": lambda s: s.pop("security_opt"),
    "no_privileged": lambda s: s.__setitem__("privileged", True),
    "restart": lambda s: s.__setitem__("restart", "unless-stopped"),
    "no_build": lambda s: s.__setitem__("build", {"context": "."}),
    "no_env_file": lambda s: s.__setitem__("env_file", [{"path": "/x.env"}]),
    "no_ports": lambda s: s.__setitem__("ports", [{"target": 8080, "published": "8080"}]),
    "no_network_mode": lambda s: s.__setitem__("network_mode", "host"),
    "no_container_name": lambda s: s.__setitem__("container_name", "bfx-bot"),
    "no_pid": lambda s: s.__setitem__("pid", "host"),
    "tmpfs": lambda s: s.__setitem__("tmpfs", ["/tmp:rw,exec"]),
    "no_extra_volumes": lambda s: s["volumes"].append(
        {"type": "bind", "source": "/", "target": "/host"}),
    "network": lambda s: s["networks"].__setitem__("other", {}),
    "runtime_env": lambda s: s["environment"].pop("BFX_EXCHANGE_ACCOUNT_ID"),
    "loader_injection_blanked": lambda s: s["environment"].pop("LD_PRELOAD"),
    "interpreter_injection_blanked": lambda s: s["environment"].pop("PYTHONPATH"),
}


@needs_compose
@pytest.mark.parametrize("prop", sorted(REMOVALS))
def test_dropping_a_property_is_detected_by_name(rendered: dict[str, Any], prop: str) -> None:
    broken = copy.deepcopy(rendered)
    REMOVALS[prop](broken["services"]["weekly-report"])
    assert f"weekly-report:{prop}" in policy.weekly_violations(broken)


# How each Compose version renders the file's reports mount (`bind:
# create_host_path: false`). v5 prints the false value; 2.38.2 (GitHub's ubuntu
# runners and the VM) omits it and leaves an empty `bind`, which failed CI on
# PR #27 when only the v5 shape was accepted.
REPORTS_MOUNT_V5 = {"type": "bind", "source": "/home/ubuntu/bfx/reports", "target": "/reports",
                    "bind": {"create_host_path": False}}
REPORTS_MOUNT_V2 = {"type": "bind", "source": "/home/ubuntu/bfx/reports", "target": "/reports",
                    "bind": {}}
TAPE_MOUNT = {"type": "bind", "source": "/home/ubuntu/bfx/tape", "target": "/tape",
              "bind": {"create_host_path": False}}
TAPE_MOUNT_RO = {**TAPE_MOUNT, "read_only": True}


def test_the_file_pins_create_host_path_false() -> None:
    assert COMPOSE["services"]["weekly-report"]["volumes"] == [REPORTS_MOUNT_V5, TAPE_MOUNT_RO]
    assert COMPOSE["services"]["tape-ingest"]["volumes"] == [TAPE_MOUNT]
    assert Path(policy.WEEKLY_REPORTS["source"]) == weekly.REPORTS_DIR
    assert Path(policy.WEEKLY_TAPE["source"]) == weekly.TAPE_DIR
    assert policy.TAPE_SERVICE == weekly.TAPE_SERVICE


@needs_compose
@pytest.mark.parametrize("mount", [REPORTS_MOUNT_V5, REPORTS_MOUNT_V2],
                         ids=["compose-v5", "compose-v2.38"])
def test_both_compose_renderings_of_the_mount_pass(rendered: dict[str, Any], mount: dict[str, Any]) -> None:
    shaped = copy.deepcopy(rendered)
    shaped["services"]["weekly-report"]["volumes"] = [copy.deepcopy(mount), dict(TAPE_MOUNT_RO)]
    assert policy.weekly_violations(shaped) == []
    without_bind = {k: v for k, v in mount.items() if k != "bind"}
    assert policy.reports_mount_ok(without_bind)


@needs_compose
@pytest.mark.parametrize("change", [
    {"bind": {"create_host_path": True}}, {"read_only": True}, {"source": "/home/ubuntu"},
    {"target": "/app"}, {"type": "volume"}, {"bind": {"propagation": "rshared"}},
    {"bind": {"selinux": "z"}}, {"volume": {"nocopy": True}}, {"bind": "yes"},
])
@pytest.mark.parametrize("base", [REPORTS_MOUNT_V5, REPORTS_MOUNT_V2], ids=["compose-v5", "compose-v2.38"])
def test_reports_mount_must_stay_exactly_the_reports_bind(
    rendered: dict[str, Any], base: dict[str, Any], change: dict[str, Any],
) -> None:
    broken = copy.deepcopy(rendered)
    broken["services"]["weekly-report"]["volumes"] = [{**copy.deepcopy(base), **change}]
    assert "weekly-report:reports_mount" in policy.weekly_violations(broken), change
    broken["services"]["weekly-report"]["volumes"] = [copy.deepcopy(base), copy.deepcopy(base)]
    assert "weekly-report:reports_mount" in policy.weekly_violations(broken)


@needs_compose
def test_project_and_external_network_are_required(rendered: dict[str, Any]) -> None:
    broken = copy.deepcopy(rendered)
    broken["name"] = "bfx"
    broken["networks"]["bfx_default"]["external"] = False
    broken["services"]["postgres"] = {}
    assert {"project_name", "external_network", "services"} <= set(policy.weekly_violations(broken))


@needs_compose
@pytest.mark.parametrize("missing", sorted(FAKE_ENV))
def test_compose_refuses_to_render_without_each_variable(missing: str) -> None:
    result = _render({k: v for k, v in FAKE_ENV.items() if k != missing})
    assert result.returncode != 0
    assert missing in result.stderr


@needs_compose
def test_the_public_site_build_file_gets_the_same_policy_with_its_own_mount() -> None:
    # bfx_public_site.py runs it on this job's plan: same variables, same hardening; its one
    # writable host path is the unit's per-run build directory.
    path = ROOT / "deploy/vm/ops/docker-compose.public-site.yml"
    result = subprocess.run([*COMPOSE_CMD, "-f", str(path), "config", "--format", "json"],
                            capture_output=True, text=True, check=False, env={**_path_env(), **FAKE_ENV})
    assert result.returncode == 0, result.stderr
    assert set(re.findall(r"\$\{([A-Z_]+):\?", path.read_text(encoding="utf-8"))) == set(FAKE_ENV)
    argv = [sys.executable, str(ROOT / "deploy/vm/ops/compose_policy.py"), "--kind", "public-site"]
    ok = subprocess.run(argv, input=result.stdout, capture_output=True, text=True, check=False)
    assert ok.returncode == 0, ok.stderr
    broken = json.loads(result.stdout)
    broken["services"]["public-site-build"]["volumes"][0]["source"] = "/home/ubuntu/bfx/reports"
    bad = subprocess.run(argv, input=json.dumps(broken), capture_output=True, text=True, check=False)
    assert bad.returncode == 1 and "public-site-build:reports_mount" in bad.stderr
    # An allowlist: a secret, a config or another container's volumes never pass.
    sneaky = json.loads(result.stdout)
    sneaky["secrets"] = {"t": {"file": "/opt/bfx/runtime/secrets/public-site/CLOUDFLARE_API_TOKEN"}}
    sneaky["services"]["public-site-build"]["secrets"] = [{"source": "t", "target": "/run/secrets/t"}]
    sneaky["services"]["public-site-build"]["volumes_from"] = ["container:bfx-postgres"]
    found = policy.weekly_violations(sneaky, project="bfx-public-site", service="public-site-build",
                                     mount=policy.PUBLIC_SITE_OUT)
    assert {"top_level_key:secrets", "public-site-build:unexpected_key:secrets",
            "public-site-build:unexpected_key:volumes_from"} <= set(found)
    assert "--kind public-site" in (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- runner


class FakeLedger:
    def __init__(self, rows: list[tuple[str, str, str]], *, exists: bool = True) -> None:
        self.rows = rows  # (revision, backend digest, outcome), oldest first
        self.exists = exists

    def _row(self, index: int) -> Any:
        revision, digest, outcome = self.rows[index]
        return bfx.LedgerRow(id=index + 1, attempt_id=f"attempt-{index}", source_revision=revision,
                             backend_digest=digest,
                             outcome=outcome, migrations_applied=False)

    def read(self) -> Any:
        successes = [i for i, row in enumerate(self.rows) if row[2] == "deployed"]
        return bfx.LedgerView(
            exists=self.exists,
            last_attempt=self._row(len(self.rows) - 1) if self.rows else None,
            last_success=self._row(successes[-1]) if successes else None)


@pytest.fixture
def host(tmp_path: Path) -> dict[str, Path]:
    release = tmp_path / "bfx-ops/releases" / REV
    (release / "ops").mkdir(parents=True)
    shutil.copy(COMPOSE_PATH, release / "ops" / weekly.COMPOSE_FILE)
    (release / ".installed").write_text(REV + "\n")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "research.env").write_text(
        "DATABASE_URL=postgresql://bfx_research:pw$x@bfx-postgres:5432/bfx\n"
        "BFX_EXCHANGE_ACCOUNT_ID=00000000-0000-4000-8000-000000000001\n")
    # The bot's file stays on the host; the runner never reads it.
    (runtime / "bot.env").write_text(
        "DATABASE_URL=postgresql://bfx_bot:bot-pw@bfx-postgres:5432/bfx\n"
        "BFX_EXCHANGE_ACCOUNT_ID=00000000-0000-4000-8000-000000000009\n"
        "BFX_VAULT_KEK=never-leaves-bot-env\nBFX_ADMIN_TOKEN=also-not\n")
    reports = tmp_path / "reports"
    reports.mkdir()
    tape = tmp_path / "tape"
    tape.mkdir()
    state = tmp_path / "state"
    for revision in (REV, OLD_REV):
        (state / "releases" / revision).mkdir(parents=True)
    return {"ops": release / "ops", "runtime": runtime, "state": state, "reports": reports,
            "tape": tape}


def _plan(host: dict[str, Path], ledger: FakeLedger, checked: list[Path] | None = None) -> Any:
    return weekly.build_plan(
        ops_dir=host["ops"], ledger=ledger, runtime_dir=host["runtime"],
        backend_repository=BACKEND, reports_dir=host["reports"], tape_dir=host["tape"],
        secret_check=(checked.append if checked is not None else lambda _path: None))


def test_plan_runs_the_deployed_digest_with_only_the_needed_env(host: dict[str, Path]) -> None:
    checked: list[Path] = []
    # A newer failed attempt never becomes the image: only `deployed` rows count.
    ledger = FakeLedger([(OLD_REV, OLD_DIGEST, "deployed"), (REV, DIGEST, "deployed"),
                         ("c" * 40, "sha256:" + "9" * 64, "failed")])
    plan = _plan(host, ledger, checked)
    assert checked == [host["runtime"] / "research.env"]  # root 0600, as bfx-deploy checks it
    assert plan.backend_image == f"{BACKEND}@{DIGEST}"
    compose = ["docker", "compose", "-p", "bfx-weekly-report", "-f",
               str(host["ops"] / "docker-compose.weekly-report.yml"), "run", "--rm"]
    assert plan.argv == [*compose, "weekly-report"]
    assert plan.describe()["pre_argvs"] == [[*compose, "tape-ingest"]]
    assert plan.env["DATABASE_URL"] == "postgresql://bfx_research:pw$x@bfx-postgres:5432/bfx"  # literal
    assert plan.env["BFX_EXCHANGE_ACCOUNT_ID"] == "00000000-0000-4000-8000-000000000001"
    assert "BFX_DEPLOYMENT_ENV" not in plan.env  # no one-shot job reads it
    assert plan.env["BFX_BACKEND_DIGEST"] == DIGEST and plan.env["BFX_SOURCE_REVISION"] == REV
    assert "BFX_VAULT_KEK" not in plan.env and "BFX_ADMIN_TOKEN" not in plan.env
    described = json.dumps(plan.describe())
    assert "bfx_research:pw" not in described and "00000000-0000-4000-8000-000000000001" not in described
    assert plan.describe()["steps_match_deployed_release"] is True


def test_steps_stay_with_the_installed_tooling_when_it_lags_the_deploy(
    host: dict[str, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    # Tooling install failed after the newer deploy: image and live.env follow the
    # deployed release, the steps are the installed ones, and the run says so.
    (host["ops"].parent / ".installed").write_text(OLD_REV + "\n")
    plan = _plan(host, FakeLedger([(REV, DIGEST, "deployed")]))
    assert plan.tooling_revision == OLD_REV and plan.deployed_revision == REV
    calls: list[tuple[list[str], Mapping[str, str]]] = []
    assert weekly.run(plan, execute=lambda argv, env: calls.append((argv, env)) or 7) == 7
    assert calls == [(plan.argv_for("tape-ingest"), plan.env), (plan.argv, plan.env)]
    assert "warning: steps from tooling release" in capsys.readouterr().err


@pytest.mark.parametrize(("returncode", "beats"), [(0, True), (1, False)])
def test_only_a_successful_run_refreshes_the_heartbeat_backup_check_reads(
    host: dict[str, Path], tmp_path: Path, returncode: int, beats: bool,
) -> None:
    plan = _plan(host, FakeLedger([(REV, DIGEST, "deployed")]))
    heartbeat = tmp_path / "state" / "weekly-report-heartbeat.json"
    assert weekly.run(plan, execute=lambda argv, env: returncode, heartbeat=heartbeat,
                      clock=lambda: 1_790_000_000.0) == returncode
    if beats:
        assert json.loads(heartbeat.read_text())["observed_at_ms"] == 1_790_000_000_000
    else:
        assert not heartbeat.exists()


def test_heartbeat_path_is_the_one_backup_check_defaults_to() -> None:
    assert f'default=Path("{weekly.HEARTBEAT}")' in (
        ROOT / "deploy/vm/ops/bfx_backup_check.py").read_text()


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda h, ledger: setattr(ledger, "rows", [(REV, DIGEST, "failed")]), "no_deployed_release"),
    (lambda h, ledger: setattr(ledger, "exists", False), "no_deployed_release"),
    (lambda h, ledger: setattr(ledger, "rows", [(REV, "latest", "deployed")]), "ledger_row_invalid"),
    (lambda h, ledger: (h["runtime"] / "research.env").write_text("DATABASE_URL=x\n"),
     "env_key_missing:research.env:BFX_EXCHANGE_ACCOUNT_ID"),
    (lambda h, ledger: (h["ops"] / "docker-compose.weekly-report.yml").unlink(), "weekly_compose_missing"),
    (lambda h, ledger: (h["ops"].parent / ".installed").unlink(), "tooling_release_unknown"),
    # Never let Docker create it: 2.x and v5 read an unset create_host_path differently.
    (lambda h, ledger: h["reports"].rmdir(), "reports_dir_missing"),
    (lambda h, ledger: (h["reports"].rmdir(), h["reports"].symlink_to(h["state"])),
     "reports_dir_missing"),
    (lambda h, ledger: (h["tape"].rmdir(), h["tape"].symlink_to(h["state"])),
     "tape_dir_not_a_directory"),
    (lambda h, ledger: (h["tape"].rmdir(), h["tape"].write_text("x")), "tape_dir_not_a_directory"),
])
def test_plan_refuses_instead_of_guessing(
    host: dict[str, Path], mutate: Callable[[dict[str, Path], FakeLedger], object], code: str,
) -> None:
    ledger = FakeLedger([(REV, DIGEST, "deployed")])
    mutate(host, ledger)
    with pytest.raises(bfx.DeployError) as excinfo:
        _plan(host, ledger)
    assert excinfo.value.code == code


def _checked_plan(host: dict[str, Path]) -> Any:
    """The real secret check (bfx-deploy's), with the test user standing in for root."""
    uid = os.getuid()
    return weekly.build_plan(
        ops_dir=host["ops"], ledger=FakeLedger([(REV, DIGEST, "deployed")]),
        runtime_dir=host["runtime"], backend_repository=BACKEND,
        reports_dir=host["reports"], tape_dir=host["tape"],
        secret_check=lambda path: bfx.protected_secret_file(path, owner_uid=uid))


def test_a_protected_research_env_is_accepted_by_the_real_check(host: dict[str, Path]) -> None:
    (host["runtime"] / "research.env").chmod(0o600)
    assert _checked_plan(host).env["DATABASE_URL"].startswith("postgresql://bfx_research:")


def test_without_research_env_the_run_stops_and_never_falls_back_to_bot_env(
    host: dict[str, Path],
) -> None:
    (host["runtime"] / "research.env").unlink()
    (host["runtime"] / "bot.env").chmod(0o600)  # a valid bot.env must not stand in
    with pytest.raises(bfx.DeployError) as excinfo:
        _checked_plan(host)
    assert excinfo.value.code == "secret_file_missing:research.env"


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o400], ids=["0644", "0640", "0400"])
def test_a_research_env_not_0600_stops_the_run(host: dict[str, Path], mode: int) -> None:
    (host["runtime"] / "research.env").chmod(mode)
    with pytest.raises(bfx.DeployError) as excinfo:
        _checked_plan(host)
    assert excinfo.value.code == "secret_file_mode_not_0600:research.env"


def test_a_symlinked_research_env_stops_the_run(host: dict[str, Path]) -> None:
    target = host["runtime"] / "elsewhere.env"
    (host["runtime"] / "research.env").rename(target)
    target.chmod(0o600)
    (host["runtime"] / "research.env").symlink_to(target)
    with pytest.raises(bfx.DeployError) as excinfo:
        _checked_plan(host)
    assert excinfo.value.code == "secret_file_missing:research.env"


def test_defaults_are_the_deployers() -> None:
    args = weekly._parser().parse_args([])
    defaults = bfx.Settings()
    assert (args.runtime_dir, args.backend_repository, args.postgres_container) == (
        defaults.runtime_dir, defaults.backend_repository, defaults.postgres_container)


# --------------------------------------------------------------------------- trade tape (plan 2026-11-15 step 2)


def test_tape_ingest_step_is_the_paced_fail_soft_collector() -> None:
    command = COMPOSE["services"]["tape-ingest"]["command"]
    assert command[:2] == ["sh", "-c"]
    (step,) = command[2].splitlines()
    assert step.startswith("timeout 2400 python -m scripts.ingest_public_trades --tape-root /tape ")
    assert "--pace" not in step  # the script's 20 s default, never faster
    assert step.endswith('finished days kept; next run resumes — non-blocking)"')
    assert "exit $$?" in step  # the WARN names the real exit code


def _timeouts(service: str) -> list[int]:
    return [int(t) for t in re.findall(r"^timeout (\d+) ", COMPOSE["services"][service]["command"][2],
                                       flags=re.M)]


def test_every_step_timeout_fits_the_unit_budget() -> None:
    unit = (ROOT / "deploy/vm/systemd/bfx-weekly-report.service").read_text(encoding="utf-8")
    match = re.search(r"(?m)^TimeoutStartSec=(\d+)$", unit)
    assert match is not None
    budget = int(match.group(1))
    total = sum(_timeouts("weekly-report")) + sum(_timeouts("tape-ingest"))
    assert total + 300 <= budget
    assert f"合計 {total}" in unit  # the unit's budget comment stays in sync
    assert _timeouts("tape-ingest") == [2400]  # 3x the measured 794 s weekly increment


@needs_compose
def test_research_service_cannot_write_the_tape(rendered: dict[str, Any]) -> None:
    broken = copy.deepcopy(rendered)
    for volume in broken["services"]["weekly-report"]["volumes"]:
        if volume["target"] == "/tape":
            volume.pop("read_only", None)
    assert "weekly-report:tape_mount_read_only" in policy.weekly_violations(broken)


@needs_compose
@pytest.mark.parametrize("change", [
    lambda s: s["volumes"][0].__setitem__("read_only", True),
    lambda s: s["volumes"].append(dict(REPORTS_MOUNT_V5)),
    lambda s: s["volumes"].clear(),
], ids=["read-only", "reports-too", "none"])
def test_tape_ingest_writes_only_the_tape(rendered: dict[str, Any], change: Callable[..., Any]) -> None:
    broken = copy.deepcopy(rendered)
    change(broken["services"]["tape-ingest"])
    found = policy.weekly_violations(broken)
    assert {"tape-ingest:tape_mount", "tape-ingest:no_extra_volumes"} & set(found), found


@needs_compose
@pytest.mark.parametrize("key", ["DATABASE_URL", "BFX_EXCHANGE_ACCOUNT_ID"])
def test_tape_ingest_gets_no_database_identity(rendered: dict[str, Any], key: str) -> None:
    assert key not in rendered["services"]["tape-ingest"]["environment"]
    broken = copy.deepcopy(rendered)
    broken["services"]["tape-ingest"]["environment"][key] = "x"
    assert "tape-ingest:no_database_env" in policy.weekly_violations(broken)


@needs_compose
def test_tape_ingest_keeps_the_hardening(rendered: dict[str, Any]) -> None:
    for prop, remove in REMOVALS.items():
        if prop == "runtime_env":
            continue
        broken = copy.deepcopy(rendered)
        remove(broken["services"]["tape-ingest"])
        assert f"tape-ingest:{prop}" in policy.weekly_violations(broken), prop


def test_a_failed_tape_ingest_is_logged_and_the_report_still_runs(
    host: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    plan = _plan(host, FakeLedger([(REV, DIGEST, "deployed")]))
    calls: list[list[str]] = []

    def execute(argv: list[str], env: Mapping[str, str]) -> int:
        calls.append(argv)
        return 1 if argv[-1] == "tape-ingest" else 0

    assert weekly.run(plan, execute=execute, heartbeat=tmp_path / "hb.json") == 0
    assert [c[-1] for c in calls] == ["tape-ingest", "weekly-report"]
    assert "warning: tape-ingest exited 1" in capsys.readouterr().err


def test_a_missing_tape_dir_is_created_not_a_reason_to_skip_the_report(host: dict[str, Path]) -> None:
    host["tape"].rmdir()
    made: list[Path] = []
    weekly.build_plan(
        ops_dir=host["ops"], ledger=FakeLedger([(REV, DIGEST, "deployed")]),
        runtime_dir=host["runtime"], backend_repository=BACKEND, reports_dir=host["reports"],
        tape_dir=host["tape"], secret_check=lambda _p: None,
        make_tape_dir=lambda p: (made.append(p), p.mkdir()))
    assert made == [host["tape"]] and host["tape"].is_dir()


def test_tape_dir_creation_failure_is_named(host: dict[str, Path]) -> None:
    host["tape"].rmdir()

    def denied(_p: Path) -> None:
        raise PermissionError

    with pytest.raises(bfx.DeployError) as excinfo:
        weekly.build_plan(
            ops_dir=host["ops"], ledger=FakeLedger([(REV, DIGEST, "deployed")]),
            runtime_dir=host["runtime"], backend_repository=BACKEND, reports_dir=host["reports"],
            tape_dir=host["tape"], secret_check=lambda _p: None, make_tape_dir=denied)
    assert excinfo.value.code == "tape_dir_unusable:PermissionError"
