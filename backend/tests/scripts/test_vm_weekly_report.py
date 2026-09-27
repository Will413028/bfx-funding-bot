"""The weekly-report job as release-managed host tooling.

deploy/vm/ops/docker-compose.weekly-report.yml holds the steps (checked in CI by
compose_policy.py --kind weekly-report on Compose's own rendering), and
deploy/vm/ops/bfx_weekly_report.py resolves what it runs: the ledger's deployed
backend digest, three env values, and the compose file of its own release.
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
    "BFX_DEPLOYMENT_ENV": "prod",
}

# The chain exactly as docker-compose.bot.yml defined it before the move
# (including report_interest): moving the job must not change a single step.
EXPECTED_STEPS = [
    'timeout 600 python -m scripts.ingest_funding_stats --symbols fUST,fUSD || echo "WARN ingest_funding_stats failed/timed out (frr arm may be stale — non-blocking)"',
    'python -m scripts.run_weekly_attribution --out "/reports/$$(date +%F)-attribution-reconciliation.md"',
    'python -m scripts.report_interest --out "/reports/$$(date +%F)-realized-interest.md" || echo "WARN report_interest failed (non-blocking)"',
    'python -m scripts.run_g3_live_validation --out "/reports/$$(date +%F)-g3-live-validation.md"',
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
    assert set(weekly.SECRET_KEYS + weekly.LIVE_KEYS) | {
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
    "reports_mount": lambda s: s["volumes"].append(
        {"type": "bind", "source": "/", "target": "/host"}),
    "network": lambda s: s["networks"].__setitem__("other", {}),
    "runtime_env": lambda s: s["environment"].pop("BFX_DEPLOYMENT_ENV"),
    "loader_injection_blanked": lambda s: s["environment"].pop("LD_PRELOAD"),
    "interpreter_injection_blanked": lambda s: s["environment"].pop("PYTHONPATH"),
}


@needs_compose
@pytest.mark.parametrize("prop", sorted(REMOVALS))
def test_dropping_a_property_is_detected_by_name(rendered: dict[str, Any], prop: str) -> None:
    broken = copy.deepcopy(rendered)
    REMOVALS[prop](broken["services"]["weekly-report"])
    assert f"weekly-report:{prop}" in policy.weekly_violations(broken)


@needs_compose
def test_reports_mount_must_not_be_created_or_read_only(rendered: dict[str, Any]) -> None:
    for change in ({"bind": {"create_host_path": True}}, {"read_only": True},
                   {"source": "/home/ubuntu"}):
        broken = copy.deepcopy(rendered)
        broken["services"]["weekly-report"]["volumes"][0].update(change)
        assert "weekly-report:reports_mount" in policy.weekly_violations(broken), change


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


# --------------------------------------------------------------------------- runner


class FakeLedger:
    def __init__(self, rows: list[tuple[str, str, str]], *, exists: bool = True) -> None:
        self.rows = rows  # (revision, backend digest, outcome), oldest first
        self.exists = exists

    def _row(self, index: int) -> Any:
        revision, digest, outcome = self.rows[index]
        return bfx.LedgerRow(id=index + 1, attempt_id=f"attempt-{index}", source_revision=revision,
                             backend_digest=digest, frontend_digest="sha256:" + "f" * 64,
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
    (runtime / "bot.env").write_text(
        "DATABASE_URL=postgresql://bfx_bot:pw$x@bfx-postgres:5432/bfx\n"
        "BFX_EXCHANGE_ACCOUNT_ID=00000000-0000-4000-8000-000000000001\n"
        "BFX_VAULT_KEK=never-leaves-bot-env\nBFX_ADMIN_TOKEN=also-not\n")
    state = tmp_path / "state"
    for revision in (REV, OLD_REV):
        (state / "releases" / revision).mkdir(parents=True)
        shutil.copy(ROOT / "deploy/vm/live.env", state / "releases" / revision / "live.env")
    return {"ops": release / "ops", "runtime": runtime, "state": state}


def _plan(host: dict[str, Path], ledger: FakeLedger, checked: list[Path] | None = None) -> Any:
    return weekly.build_plan(
        ops_dir=host["ops"], ledger=ledger, runtime_dir=host["runtime"], state_dir=host["state"],
        backend_repository=BACKEND,
        secret_check=(checked.append if checked is not None else lambda _path: None))


def test_plan_runs_the_deployed_digest_with_only_the_needed_env(host: dict[str, Path]) -> None:
    checked: list[Path] = []
    # A newer failed attempt never becomes the image: only `deployed` rows count.
    ledger = FakeLedger([(OLD_REV, OLD_DIGEST, "deployed"), (REV, DIGEST, "deployed"),
                         ("c" * 40, "sha256:" + "9" * 64, "failed")])
    plan = _plan(host, ledger, checked)
    assert checked == [host["runtime"] / "bot.env"]  # root 0600, as bfx-deploy checks it
    assert plan.backend_image == f"{BACKEND}@{DIGEST}"
    assert plan.argv == ["docker", "compose", "-p", "bfx-weekly-report", "-f",
                         str(host["ops"] / "docker-compose.weekly-report.yml"),
                         "run", "--rm", "weekly-report"]
    assert plan.env["DATABASE_URL"] == "postgresql://bfx_bot:pw$x@bfx-postgres:5432/bfx"  # literal
    assert plan.env["BFX_EXCHANGE_ACCOUNT_ID"] == "00000000-0000-4000-8000-000000000001"
    assert plan.env["BFX_DEPLOYMENT_ENV"] == "prod"
    assert plan.env["BFX_BACKEND_DIGEST"] == DIGEST and plan.env["BFX_SOURCE_REVISION"] == REV
    assert "BFX_VAULT_KEK" not in plan.env and "BFX_ADMIN_TOKEN" not in plan.env
    described = json.dumps(plan.describe())
    assert "bfx_bot:pw" not in described and "00000000-0000-4000-8000-000000000001" not in described
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
    assert calls == [(plan.argv, plan.env)]
    assert "warning: steps from tooling release" in capsys.readouterr().err


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda h, ledger: setattr(ledger, "rows", [(REV, DIGEST, "failed")]), "no_deployed_release"),
    (lambda h, ledger: setattr(ledger, "exists", False), "no_deployed_release"),
    (lambda h, ledger: setattr(ledger, "rows", [(REV, "latest", "deployed")]), "ledger_row_invalid"),
    (lambda h, ledger: (h["runtime"] / "bot.env").write_text("DATABASE_URL=x\n"),
     "env_key_missing:bot.env:BFX_EXCHANGE_ACCOUNT_ID"),
    (lambda h, ledger: (h["state"] / "releases" / REV / "live.env").unlink(),
     "env_file_unreadable:live.env"),
    (lambda h, ledger: (h["ops"] / "docker-compose.weekly-report.yml").unlink(), "weekly_compose_missing"),
    (lambda h, ledger: (h["ops"].parent / ".installed").unlink(), "tooling_release_unknown"),
])
def test_plan_refuses_instead_of_guessing(
    host: dict[str, Path], mutate: Callable[[dict[str, Path], FakeLedger], object], code: str,
) -> None:
    ledger = FakeLedger([(REV, DIGEST, "deployed")])
    mutate(host, ledger)
    with pytest.raises(bfx.DeployError) as excinfo:
        _plan(host, ledger)
    assert excinfo.value.code == code


def test_an_unprotected_bot_env_stops_the_run(host: dict[str, Path]) -> None:
    def refuse(path: Path) -> None:
        raise bfx.DeployError(f"secret_file_mode_not_0600:{path.name}")

    with pytest.raises(bfx.DeployError, match=r"secret_file_mode_not_0600:bot\.env"):
        weekly.build_plan(ops_dir=host["ops"], ledger=FakeLedger([(REV, DIGEST, "deployed")]),
                          runtime_dir=host["runtime"], state_dir=host["state"],
                          backend_repository=BACKEND, secret_check=refuse)


def test_defaults_are_the_deployers() -> None:
    args = weekly._parser().parse_args([])
    defaults = bfx.Settings()
    assert (args.runtime_dir, args.state_dir, args.backend_repository, args.postgres_container) == (
        defaults.runtime_dir, defaults.state_dir, defaults.backend_repository,
        defaults.postgres_container)
