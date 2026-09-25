"""deploy/vm/docker-compose.app.yml, checked the way CI checks it.

The hardening policy (deploy/vm/ops/compose_policy.py) runs on what `docker
compose config` renders, not on the YAML text; each property must fail by name
when removed. The last test creates (never starts) the three containers from
the real file and proves bfx-deploy's post-up check accepts them.
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
import uuid
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PATH = ROOT / "deploy/vm/docker-compose.app.yml"
COMPOSE_TEXT = COMPOSE_PATH.read_text(encoding="utf-8")
COMPOSE = yaml.safe_load(COMPOSE_TEXT)
DIGEST_B = "sha256:" + "3" * 64
DIGEST_F = "sha256:" + "4" * 64
FAKE_ENV = {
    "BFX_BACKEND_IMAGE": f"ghcr.io/will413028/bfx-funding-bot-backend@{DIGEST_B}",
    "BFX_BACKEND_DIGEST": DIGEST_B,
    "BFX_FRONTEND_IMAGE": f"ghcr.io/will413028/bfx-funding-bot-frontend@{DIGEST_F}",
    "BFX_FRONTEND_DIGEST": DIGEST_F,
    "BFX_SOURCE_REVISION": "b" * 40,
    "BFX_CHANGE_CLASS": "standard",
    "BFX_DEPLOYMENT_ID": "0b8f7c5e-5d59-4a4c-9b58-6f0a1c2d3e4f",
}


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


policy = _load("vm_ops_compose_policy_under_test", ROOT / "deploy/vm/ops/compose_policy.py")
bfx = _load("vm_ops_bfx_deploy_for_compose", ROOT / "deploy/vm/ops/bfx_deploy.py")


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True,
                          check=False).returncode == 0


needs_compose = pytest.mark.skipif(not _compose_available(), reason="docker compose CLI unavailable")


def _path_env() -> dict[str, str]:
    return {key: os.environ[key] for key in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT")
            if key in os.environ}


@pytest.fixture(scope="module")
def runtime_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Empty stand-ins for /opt/bfx/runtime: older Compose stats env files even
    with --no-env-resolution, and CI has no secret directory."""
    directory = tmp_path_factory.mktemp("runtime")
    for name in ("bot.env", "webapi.env", "frontend.env"):
        (directory / name).write_text("")
    return directory


def _render(runtime: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_PATH), "config", "--no-env-resolution",
         "--format", "json"],
        capture_output=True, text=True, check=False,
        env={**_path_env(), **(FAKE_ENV if env is None else env), "BFX_RUNTIME_DIR": str(runtime)})


@pytest.fixture(scope="module")
def rendered(runtime_dir: Path) -> dict[str, Any]:
    if not _compose_available():
        pytest.skip("docker compose CLI unavailable")
    result = _render(runtime_dir)
    assert result.returncode == 0, result.stderr
    return dict(json.loads(result.stdout))


# --------------------------------------------------------------------------- file shape


def test_only_the_three_application_services_are_defined() -> None:
    assert set(COMPOSE["services"]) == {"bot", "webapi", "frontend"}
    assert COMPOSE["name"] == "bfx-app"
    assert "volumes" not in COMPOSE


def test_postgres_and_redis_are_never_part_of_this_project() -> None:
    for word in ("postgres", "redis", "pgdata", "redisdata"):
        assert word not in json.dumps(COMPOSE["services"])


def test_no_image_tag_appears_anywhere_in_the_file() -> None:
    images = [spec["image"] for spec in COMPOSE["services"].values()]
    assert all(image.startswith("${") for image in images)
    assert re.search(r"ghcr\.io/\S+:(main|sha-|latest)", COMPOSE_TEXT) is None


def test_runtime_env_files_default_to_the_production_secret_directory() -> None:
    assert policy.source_violations(COMPOSE_TEXT) == []
    assert all(entry.startswith("${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/") or entry == "live.env"
               for spec in COMPOSE["services"].values() for entry in spec["env_file"])


@pytest.mark.parametrize("edit", [
    lambda t: t.replace("${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/bot.env", "${BFX_RUNTIME_DIR:-/tmp}/bot.env"),
    lambda t: t.replace("${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/webapi.env", "${BFX_RUNTIME_DIR}/webapi.env"),
    lambda t: t.replace("      - ${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/frontend.env",
                        "      - path: ${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/frontend.env\n"
                        "        required: false"),
])
def test_changing_the_default_or_making_secrets_optional_is_detected(edit: Callable[[str], str]) -> None:
    assert policy.source_violations(edit(COMPOSE_TEXT)) != []


def test_policy_and_deployer_agree_on_names_and_commands() -> None:
    assert policy.CONTAINERS == bfx.CONTAINERS
    assert tuple(bfx.identity_env(
        bfx.Target("b" * 40, DIGEST_B, DIGEST_F, "x", "y"), service="bot", klass="standard",
        deployment_id="d")) == policy.IDENTITY


# --------------------------------------------------------------------------- rendered policy


@needs_compose
def test_rendered_compose_file_satisfies_the_policy(rendered: dict[str, Any], runtime_dir: Path) -> None:
    assert policy.violations(rendered, str(runtime_dir)) == []
    # Rendered against the production directory, the stand-in paths are a violation.
    assert {f"{s}:env_files" for s in ("bot", "webapi", "frontend")} <= set(policy.violations(rendered))


@needs_compose
def test_policy_cli_is_what_ci_runs(rendered: dict[str, Any], runtime_dir: Path) -> None:
    argv = [sys.executable, str(ROOT / "deploy/vm/ops/compose_policy.py"), "--source", str(COMPOSE_PATH)]
    env = {**_path_env(), "BFX_RUNTIME_DIR": str(runtime_dir)}
    completed = subprocess.run(argv, input=json.dumps(rendered), capture_output=True, text=True,
                               check=False, env=env)
    assert completed.returncode == 0, completed.stderr
    broken = copy.deepcopy(rendered)
    broken["services"]["bot"]["read_only"] = False
    completed = subprocess.run(argv, input=json.dumps(broken), capture_output=True, text=True,
                               check=False, env=env)
    assert completed.returncode == 1 and "bot:read_only" in completed.stderr


REMOVALS: dict[str, Callable[[dict[str, Any]], None]] = {
    "image_digest_reference": lambda s: s.__setitem__("image", "ghcr.io/x/y:main"),
    "pull_policy_never": lambda s: s.pop("pull_policy"),
    "no_build": lambda s: s.__setitem__("build", {"context": "."}),
    "container_name": lambda s: s.pop("container_name"),
    "entrypoint_cleared": lambda s: s.pop("entrypoint"),
    "exact_command": lambda s: s.__setitem__("command", ["sh", "-c", "x"]),
    "user": lambda s: s.pop("user"),
    "workdir": lambda s: s.pop("working_dir"),
    "read_only": lambda s: s.pop("read_only"),
    "cap_drop_all": lambda s: s.pop("cap_drop"),
    "no_cap_add": lambda s: s.__setitem__("cap_add", ["NET_ADMIN"]),
    "no_new_privileges": lambda s: s.pop("security_opt"),
    "no_privileged": lambda s: s.__setitem__("privileged", True),
    "restart": lambda s: s.pop("restart"),
    "network": lambda s: s["networks"].__setitem__("other", {}),
    "no_network_mode": lambda s: s.__setitem__("network_mode", "host"),
    "no_volumes": lambda s: s.__setitem__("volumes", [{"type": "bind", "source": "/", "target": "/host"}]),
    "no_tmpfs": lambda s: s.__setitem__("tmpfs", ["/tmp"]),
    "no_pid": lambda s: s.__setitem__("pid", "host"),
    "ports": lambda s: s.__setitem__("ports", [{"target": 3000, "published": "3001"}]),
    "env_files": lambda s: s.__setitem__("env_file", [{"path": "/root/.env"}]),
    "identity_env": lambda s: s["environment"].pop("BFX_DEPLOYMENT_ID"),
    "loader_injection_blanked": lambda s: s["environment"].pop("LD_PRELOAD"),
    "interpreter_injection_blanked": lambda s: s["environment"].pop(
        "NODE_OPTIONS" if "NODE_OPTIONS" in s["environment"] else "PYTHONPATH"),
}


@needs_compose
@pytest.mark.parametrize("service", ["bot", "webapi", "frontend"])
@pytest.mark.parametrize("prop", sorted(REMOVALS))
def test_dropping_a_property_is_detected_by_name(
    rendered: dict[str, Any], runtime_dir: Path, service: str, prop: str,
) -> None:
    broken = copy.deepcopy(rendered)
    REMOVALS[prop](broken["services"][service])
    assert f"{service}:{prop}" in policy.violations(broken, str(runtime_dir))


@needs_compose
def test_bot_alias_and_external_network_are_required(rendered: dict[str, Any]) -> None:
    broken = copy.deepcopy(rendered)
    broken["services"]["bot"]["networks"]["bfx_default"] = {}
    broken["networks"]["bfx_default"]["external"] = False
    assert {"bot:bot_alias", "external_network"} <= set(policy.violations(broken))


@needs_compose
@pytest.mark.parametrize("missing", sorted(FAKE_ENV))
def test_compose_refuses_to_render_without_each_deploy_variable(runtime_dir: Path, missing: str) -> None:
    result = _render(runtime_dir, {k: v for k, v in FAKE_ENV.items() if k != missing})
    assert result.returncode != 0
    assert missing in result.stderr


# --------------------------------------------------------------------------- created containers


def _local_image_with_repo_digest() -> str | None:
    """Any locally present image that has a registry digest (no pull, no build)."""
    listed = subprocess.run(["docker", "image", "ls", "--digests", "--format",
                             "{{.Repository}}@{{.Digest}}"], capture_output=True, text=True,
                            check=False, timeout=30)
    if listed.returncode != 0:
        return None
    for line in listed.stdout.split():
        if re.fullmatch(r"[a-z0-9._/-]+@sha256:[0-9a-f]{64}", line):
            return line
    return None


@pytest.mark.integration
@needs_compose
def test_compose_created_containers_pass_the_deploy_check(tmp_path: Path) -> None:
    """Create (never start) the three services from the real file and check them.

    A local image that already has a repository digest stands in for both
    application images; network and container names are made unique so nothing
    real is touched (so only name and project differ from what bfx-deploy expects).
    """
    image = _local_image_with_repo_digest()
    if image is None:
        pytest.skip("no local image with a repository digest")
    suffix = uuid.uuid4().hex[:10]
    network = f"bfx-test-{suffix}"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    for name in ("bot.env", "webapi.env", "frontend.env"):
        (runtime / name).write_text("EXAMPLE=1\n")
    text = (COMPOSE_TEXT.replace("name: bfx_default", f"name: {network}")
            .replace("name: bfx-app", f"name: bfx-app-test-{suffix}"))
    for container in ("bfx-bot", "bfx-webapi", "bfx-frontend"):
        text = text.replace(f"container_name: {container}", f"container_name: {container}-{suffix}")
    (tmp_path / "docker-compose.app.yml").write_text(text)
    shutil.copy(ROOT / "deploy/vm/live.env", tmp_path / "live.env")
    digest = image.split("@", 1)[1]
    target = bfx.Target(FAKE_ENV["BFX_SOURCE_REVISION"], digest, digest, image, image)
    env = {**_path_env(), **FAKE_ENV, "BFX_RUNTIME_DIR": str(runtime),
           "BFX_BACKEND_IMAGE": image, "BFX_FRONTEND_IMAGE": image,
           "BFX_BACKEND_DIGEST": digest, "BFX_FRONTEND_DIGEST": digest}
    project = f"bfx-app-test-{suffix}"
    compose = ["docker", "compose", "-p", project, "-f", str(tmp_path / "docker-compose.app.yml")]
    subprocess.run(["docker", "network", "create", network], check=True, capture_output=True)
    try:
        created = subprocess.run([*compose, "create", "--no-recreate"], capture_output=True,
                                 text=True, check=False, env=env, timeout=120)
        assert created.returncode == 0, created.stderr
        names = [f"{c}-{suffix}" for c in ("bfx-bot", "bfx-webapi", "bfx-frontend")]
        inspected = subprocess.run(["docker", "inspect", *names], capture_output=True, text=True,
                                   check=True)
        for service, record in zip(("bot", "webapi", "frontend"), json.loads(inspected.stdout),
                                   strict=True):
            mismatches = bfx.container_mismatches(
                record, service=service, target=target, klass="standard",
                deployment_id=FAKE_ENV["BFX_DEPLOYMENT_ID"])
            assert mismatches == ["name", "project"], (service, mismatches)
    finally:
        subprocess.run([*compose, "down", "--remove-orphans"], capture_output=True, env=env,
                       check=False, timeout=120)
        subprocess.run(["docker", "network", "rm", network], capture_output=True, check=False)
