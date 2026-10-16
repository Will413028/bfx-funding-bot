"""deploy/vm/ops/docker-compose.operator.yml, checked the way CI checks it.

The operator CLI's one-shot container is enforced by compose_policy.py --kind
operator on Compose's own renderings: the interpolated one for the hardening,
the `--no-interpolate` one for the env file and the wrapper's required
variables (Compose 2.38 folds env_file into `environment` when interpolating).
Each property must fail by name when removed.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

# One Compose probe and skip marker for every VM compose test.
from tests.scripts.test_vm_compose_app import COMPOSE_CMD, needs_compose

ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PATH = ROOT / "deploy/vm/ops/docker-compose.operator.yml"
POLICY_PATH = ROOT / "deploy/vm/ops/compose_policy.py"
COMPOSE_TEXT = COMPOSE_PATH.read_text(encoding="utf-8")
COMPOSE = yaml.safe_load(COMPOSE_TEXT)
SERVICE = "operator"
DIGEST = "sha256:" + "3" * 64
FAKE_ENV = {
    "BFX_BACKEND_IMAGE": f"ghcr.io/will413028/bfx-funding-bot-backend@{DIGEST}",
    "BFX_BACKEND_DIGEST": DIGEST,
    "BFX_SOURCE_REVISION": "b" * 40,
    "BFX_OPERATOR_TAILNET_IDENTITY": "operator@example.test",
    "BFX_DEPLOYMENT_ENV": "prod",
}


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


policy = _load("vm_ops_compose_policy_for_operator", POLICY_PATH)

def _path_env() -> dict[str, str]:
    return {key: os.environ[key] for key in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT")
            if key in os.environ}


@pytest.fixture(scope="module")
def runtime_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An empty stand-in for /opt/bfx/runtime/operator.env, as CI uses."""
    directory = tmp_path_factory.mktemp("runtime")
    (directory / "operator.env").write_text("")
    return directory


def _render(runtime: Path, env: Mapping[str, str] | None = None, *, path: Path = COMPOSE_PATH,
            mode: str = "--no-env-resolution") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*COMPOSE_CMD, "-f", str(path), "config", mode, "--format", "json"],
        capture_output=True, text=True, check=False,
        env={**_path_env(), **(FAKE_ENV if env is None else env), "BFX_RUNTIME_DIR": str(runtime)})


def _rendered(runtime: Path, path: Path = COMPOSE_PATH) -> dict[str, Any]:
    result = _render(runtime, path=path)
    assert result.returncode == 0, result.stderr
    return dict(json.loads(result.stdout))


def _uninterpolated(runtime: Path, path: Path = COMPOSE_PATH) -> dict[str, Any]:
    result = _render(runtime, path=path, mode="--no-interpolate")
    assert result.returncode == 0, result.stderr
    return dict(json.loads(result.stdout))


@pytest.fixture(scope="module")
def rendered(runtime_dir: Path) -> dict[str, Any]:
    return _rendered(runtime_dir)


@pytest.fixture(scope="module")
def uninterpolated(runtime_dir: Path) -> dict[str, Any]:
    return _uninterpolated(runtime_dir)


def _all_violations(rendered: Mapping[str, Any], uninterpolated: Mapping[str, Any]) -> list[str]:
    return [*policy.operator_violations(rendered),
            *policy.operator_uninterpolated_violations(uninterpolated)]


# --------------------------------------------------------------------------- file shape


def test_one_operator_service_on_a_digest_image_with_no_tag() -> None:
    assert COMPOSE["name"] == "bfx-operator"
    assert set(COMPOSE["services"]) == {SERVICE}
    assert COMPOSE["services"][SERVICE]["image"].startswith("${")
    assert re.search(r"ghcr\.io/\S+:(main|sha-|latest)", COMPOSE_TEXT) is None
    required = set(re.findall(r"\$\{([A-Z_]+):\?", COMPOSE_TEXT))
    assert required == set(FAKE_ENV)


def test_database_url_only_comes_from_the_operator_env_file() -> None:
    assert "DATABASE_URL" not in COMPOSE["services"][SERVICE]["environment"]
    assert "DATABASE_URL" not in policy.OPERATOR_ENV_KEYS


# --------------------------------------------------------------------------- rendered policy


@needs_compose
def test_rendered_file_satisfies_the_operator_policy(
    rendered: dict[str, Any], uninterpolated: dict[str, Any],
) -> None:
    assert _all_violations(rendered, uninterpolated) == []
    (entry,) = uninterpolated["services"][SERVICE]["env_file"]
    assert entry["path"].endswith("/${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/operator.env")
    assert entry["required"] is True


@needs_compose
def test_policy_cli_operator_kind_is_what_ci_runs(
    rendered: dict[str, Any], uninterpolated: dict[str, Any], tmp_path: Path,
) -> None:
    snapshot = tmp_path / "uninterpolated.json"
    snapshot.write_text(json.dumps(uninterpolated))
    argv = [sys.executable, str(POLICY_PATH), "--kind", "operator", "--uninterpolated", str(snapshot)]
    env = _path_env()

    def run(stdin: Mapping[str, Any]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, input=json.dumps(stdin), capture_output=True, text=True,
                              check=False, env=env)

    ok = run(rendered)
    assert ok.returncode == 0, ok.stderr
    broken = copy.deepcopy(rendered)
    broken["services"][SERVICE]["ports"] = [{"target": 8080, "published": "8080"}]
    bad = run(broken)
    assert bad.returncode == 1 and "operator:no_ports" in bad.stderr
    unrequired = copy.deepcopy(uninterpolated)
    unrequired["services"][SERVICE]["env_file"][0]["required"] = False
    snapshot.write_text(json.dumps(unrequired))
    bad = run(rendered)
    assert bad.returncode == 1 and "operator:env_files_required" in bad.stderr
    # --kind operator insists on its uninterpolated rendering, like --kind app.
    missing = subprocess.run([sys.executable, str(POLICY_PATH), "--kind", "operator"],
                             input="{}", capture_output=True, text=True, check=False)
    assert missing.returncode == 2 and "--uninterpolated" in missing.stderr


REMOVALS: dict[str, Callable[[dict[str, Any]], None]] = {
    "image_digest_reference": lambda s: s.__setitem__("image", "ghcr.io/x/y:main"),
    "pull_policy_never": lambda s: s.pop("pull_policy"),
    "exact_entrypoint": lambda s: s.__setitem__("entrypoint", ["sh", "-c", "x"]),
    "user": lambda s: s.pop("user"),
    "workdir": lambda s: s.pop("working_dir"),
    "read_only": lambda s: s.pop("read_only"),
    "cap_drop_all": lambda s: s.pop("cap_drop"),
    "no_cap_add": lambda s: s.__setitem__("cap_add", ["NET_ADMIN"]),
    "no_new_privileges": lambda s: s.pop("security_opt"),
    "no_privileged": lambda s: s.__setitem__("privileged", True),
    "restart": lambda s: s.__setitem__("restart", "unless-stopped"),
    "no_build": lambda s: s.__setitem__("build", {"context": "."}),
    "no_ports": lambda s: s.__setitem__("ports", [{"target": 8080, "published": "8080"}]),
    "no_volumes": lambda s: s.__setitem__("volumes", [{"type": "bind", "source": "/", "target": "/host"}]),
    "no_tmpfs": lambda s: s.__setitem__("tmpfs", ["/tmp"]),
    "no_devices": lambda s: s.__setitem__("devices", ["/dev/kmsg"]),
    "no_network_mode": lambda s: s.__setitem__("network_mode", "host"),
    "no_pid": lambda s: s.__setitem__("pid", "host"),
    "no_container_name": lambda s: s.__setitem__("container_name", "bfx-operator"),
    "no_command": lambda s: s.__setitem__("command", ["status"]),
    "network": lambda s: s["networks"].__setitem__("other", {}),
    "environment_keys": lambda s: s["environment"].__setitem__("DATABASE_URL", "postgresql://x"),
    "runtime_env": lambda s: s["environment"].__setitem__("BFX_OPERATOR_TAILNET_IDENTITY", ""),
    "loader_injection_blanked": lambda s: s["environment"].__setitem__("LD_PRELOAD", "/x.so"),
    "interpreter_injection_blanked": lambda s: s["environment"].__setitem__("PYTHONPATH", "/tmp"),
}


@needs_compose
@pytest.mark.parametrize("prop", sorted(REMOVALS))
def test_dropping_a_property_is_detected_by_name(rendered: dict[str, Any], prop: str) -> None:
    broken = copy.deepcopy(rendered)
    REMOVALS[prop](broken["services"][SERVICE])
    assert f"operator:{prop}" in policy.operator_violations(broken)


@needs_compose
@pytest.mark.parametrize("key", sorted(policy.OPERATOR_INTERPOLATED))
def test_missing_wrapper_variable_is_detected(rendered: dict[str, Any], key: str) -> None:
    broken = copy.deepcopy(rendered)
    broken["services"][SERVICE]["environment"].pop(key)
    found = policy.operator_violations(broken)
    assert {"operator:environment_keys", "operator:runtime_env"} <= set(found)


@needs_compose
def test_project_services_and_external_network_are_required(rendered: dict[str, Any]) -> None:
    broken = copy.deepcopy(rendered)
    broken["name"] = "bfx"
    broken["networks"]["bfx_default"]["external"] = False
    broken["services"]["bot"] = {}
    broken["volumes"] = {"data": {}}
    assert {"project_name", "external_network", "services", "volumes"} <= set(
        policy.operator_violations(broken))


@needs_compose
def test_an_extra_network_is_detected(rendered: dict[str, Any]) -> None:
    broken = copy.deepcopy(rendered)
    broken["networks"]["other"] = {"name": "other"}
    assert "external_network" in policy.operator_violations(broken)


# Each edit is applied to the YAML text and rendered by Compose, so the check is
# on what Compose itself makes of the change (both renderings).
TEXT_MUTATIONS = [
    ("    pull_policy: never\n", '    pull_policy: never\n    ports: ["8080:8080"]\n', "operator:no_ports"),
    ("    pull_policy: never\n", "    pull_policy: never\n    volumes: [/:/host]\n", "operator:no_volumes"),
    ("    pull_policy: never\n", "    pull_policy: never\n    tmpfs: [/tmp]\n", "operator:no_tmpfs"),
    ('    restart: "no"\n', "    restart: unless-stopped\n", "operator:restart"),
    ("    read_only: true\n", "", "operator:read_only"),
    ("    cap_drop: [ALL]\n", "", "operator:cap_drop_all"),
    ('      PYTHONPATH: ""\n', '      PYTHONPATH: "/tmp"\n', "operator:interpreter_injection_blanked"),
    ('"bfx_funding_bot.apps.operator_cli"]', '"bfx_funding_bot.apps.bot"]', "operator:exact_entrypoint"),
    ("  operator:\n", "  extra:\n    image: busybox\n  operator:\n", "services"),
    ("      bfx_default: {}\n", "      bfx_default: {}\n      other: {}\n",
     "operator:network"),
    ("        required: true\n", "        required: false\n", "operator:env_files_required"),
    ("${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/operator.env", "${BFX_RUNTIME_DIR:-/tmp}/operator.env",
     "operator:env_files"),
    ("    env_file:\n      - path: ${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/operator.env\n"
     "        required: true\n", "", "operator:env_files"),
    ("${BFX_OPERATOR_TAILNET_IDENTITY:?BFX_OPERATOR_TAILNET_IDENTITY is required}",
     "${BFX_OPERATOR_TAILNET_IDENTITY:-nobody}", "operator:required_interpolation"),
    ("      BFX_OPERATOR_TAILNET_IDENTITY: ${BFX_OPERATOR_TAILNET_IDENTITY:?"
     "BFX_OPERATOR_TAILNET_IDENTITY is required}\n", "", "operator:environment_keys"),
]


@needs_compose
@pytest.mark.parametrize(("old", "new", "violation"), TEXT_MUTATIONS,
                         ids=[violation + f"-{i}" for i, (_, _, violation) in enumerate(TEXT_MUTATIONS)])
def test_edits_to_the_file_are_detected_on_compose_renderings(
    tmp_path: Path, runtime_dir: Path, old: str, new: str, violation: str,
) -> None:
    assert COMPOSE_TEXT.count(old) == 1, old
    mutated = tmp_path / "docker-compose.operator.yml"
    text = COMPOSE_TEXT.replace(old, new)
    if "other: {}" in new:
        text = text.replace("networks:\n  bfx_default:\n",
                            "networks:\n  other:\n    name: other\n  bfx_default:\n")
    mutated.write_text(text)
    assert violation in _all_violations(_rendered(runtime_dir, mutated),
                                        _uninterpolated(runtime_dir, mutated))


@needs_compose
@pytest.mark.parametrize("missing", sorted(FAKE_ENV))
def test_compose_refuses_to_render_without_each_variable(runtime_dir: Path, missing: str) -> None:
    result = _render(runtime_dir, {k: v for k, v in FAKE_ENV.items() if k != missing})
    assert result.returncode != 0
    assert missing in result.stderr


@needs_compose
def test_a_missing_operator_env_file_stops_the_render(tmp_path: Path) -> None:
    """`required: true` in effect: without operator.env Compose refuses, never runs bare."""
    completed = subprocess.run(
        [*COMPOSE_CMD, "-f", str(COMPOSE_PATH), "config", "--format", "json"],
        capture_output=True, text=True, check=False,
        env={**_path_env(), **FAKE_ENV, "BFX_RUNTIME_DIR": str(tmp_path)})
    assert completed.returncode != 0
    assert "operator.env" in completed.stderr
