"""deploy/vm/docker-compose.app.yml keeps every launcher hardening property.

One assertion per (service, property) so a single dropped line fails by name.
The hardening list is the one the retired immutable-release launcher enforced
before the move to Compose.
"""
from __future__ import annotations

import copy
import importlib.util
import json
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
}
INJECTION_KEYS = ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT")
PYTHON_INJECTION_KEYS = ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE")


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bfx = _load("vm_ops_bfx_deploy_for_compose", ROOT / "deploy/vm/ops/bfx_deploy.py")

EXPECTED = {
    "bot": {"container": "bfx-bot", "user": "1000:1000", "image": "BFX_BACKEND_IMAGE",
            "digest": "BFX_BACKEND_DIGEST", "command": list(bfx.BACKEND_COMMANDS["bot"]),
            "env_file": ["/opt/bfx/runtime/bot.env", "live.env"]},
    "webapi": {"container": "bfx-webapi", "user": "1000:1000", "image": "BFX_BACKEND_IMAGE",
               "digest": "BFX_BACKEND_DIGEST", "command": list(bfx.BACKEND_COMMANDS["webapi"]),
               "env_file": ["/opt/bfx/runtime/webapi.env"]},
    "frontend": {"container": "bfx-frontend", "user": "nextjs", "image": "BFX_FRONTEND_IMAGE",
                 "digest": "BFX_FRONTEND_DIGEST", "command": list(bfx.FRONTEND_COMMAND),
                 "env_file": ["/opt/bfx/runtime/frontend.env"]},
}


def _check(service: str, spec: dict[str, Any], prop: str) -> bool:
    expected = EXPECTED[service]
    env = spec.get("environment") or {}
    if prop == "image_is_env_digest_reference":
        pattern = r"\$\{" + expected["image"] + r":\?[^}]+\}"
        return re.fullmatch(pattern, str(spec.get("image"))) is not None
    if prop == "pull_policy_never":
        return spec.get("pull_policy") == "never"
    if prop == "no_build":
        return "build" not in spec
    if prop == "container_name":
        return spec.get("container_name") == expected["container"]
    if prop == "entrypoint_cleared":
        return spec.get("entrypoint") == []
    if prop == "exact_command":
        return spec.get("command") == expected["command"]
    if prop == "user":
        return spec.get("user") == expected["user"]
    if prop == "workdir":
        return spec.get("working_dir") == "/app"
    if prop == "read_only":
        return spec.get("read_only") is True
    if prop == "cap_drop_all":
        return spec.get("cap_drop") == ["ALL"]
    if prop == "no_cap_add":
        return "cap_add" not in spec
    if prop == "no_new_privileges":
        return spec.get("security_opt") == ["no-new-privileges:true"]
    if prop == "not_privileged":
        return not spec.get("privileged")
    if prop == "restart":
        return spec.get("restart") == "unless-stopped"
    if prop == "network":
        return set(spec.get("networks") or {}) == {"bfx_default"} and "network_mode" not in spec
    if prop == "no_volumes":
        return not spec.get("volumes") and not spec.get("tmpfs") and not spec.get("devices")
    if prop == "ports":
        wanted = ["127.0.0.1:3001:3000"] if service == "frontend" else None
        return spec.get("ports") == wanted and "expose" not in spec
    if prop == "env_files":
        return spec.get("env_file") == expected["env_file"]
    if prop == "identity_env":
        return (env.get("BFX_IMAGE_DIGEST"), env.get("BFX_SOURCE_REVISION"), env.get("BFX_CHANGE_CLASS")) == (
            f"${{{expected['digest']}:?{expected['digest']} is required}}",
            "${BFX_SOURCE_REVISION:?BFX_SOURCE_REVISION is required}",
            "${BFX_CHANGE_CLASS:?BFX_CHANGE_CLASS is required}",
        )
    if prop == "loader_injection_blanked":
        return all(env.get(key) == "" for key in INJECTION_KEYS)
    if prop == "interpreter_injection_blanked":
        if service == "frontend":
            return env.get("NODE_OPTIONS") == ""
        return all(env.get(key) == "" for key in PYTHON_INJECTION_KEYS) and env.get(
            "PYTHONDONTWRITEBYTECODE") == "1"
    if prop == "no_host_namespaces":
        return not any(spec.get(key) for key in ("pid", "ipc", "userns_mode", "cgroup", "network_mode"))
    raise AssertionError(prop)


PROPERTIES = (
    "image_is_env_digest_reference", "pull_policy_never", "no_build", "container_name",
    "entrypoint_cleared", "exact_command", "user", "workdir", "read_only", "cap_drop_all",
    "no_cap_add", "no_new_privileges", "not_privileged", "restart", "network", "no_volumes",
    "ports", "env_files", "identity_env", "loader_injection_blanked",
    "interpreter_injection_blanked", "no_host_namespaces",
)


def test_only_the_three_application_services_are_defined() -> None:
    assert set(COMPOSE["services"]) == {"bot", "webapi", "frontend"}
    assert COMPOSE["name"] == "bfx-app"
    assert "volumes" not in COMPOSE


def test_postgres_and_redis_are_never_part_of_this_project() -> None:
    for word in ("postgres", "redis", "pgdata", "redisdata"):
        assert word not in json.dumps(COMPOSE["services"])


def test_network_is_the_existing_external_one() -> None:
    assert COMPOSE["networks"] == {"bfx_default": {"external": True, "name": "bfx_default"}}


def test_bot_keeps_its_dns_alias() -> None:
    assert COMPOSE["services"]["bot"]["networks"]["bfx_default"]["aliases"] == ["bfx-bot"]


def test_no_image_tag_appears_anywhere_in_the_file() -> None:
    images = [spec["image"] for spec in COMPOSE["services"].values()]
    assert all(image.startswith("${") for image in images)
    assert re.search(r"ghcr\.io/\S+:(main|sha-|latest)", COMPOSE_TEXT) is None


@pytest.mark.parametrize("service", ["bot", "webapi", "frontend"])
@pytest.mark.parametrize("prop", PROPERTIES)
def test_service_keeps_hardening_property(service: str, prop: str) -> None:
    assert _check(service, COMPOSE["services"][service], prop), f"{service}: {prop}"


REMOVALS: dict[str, Callable[[dict[str, Any]], None]] = {
    "image_is_env_digest_reference": lambda s: s.__setitem__("image", "ghcr.io/x/y:main"),
    "pull_policy_never": lambda s: s.pop("pull_policy"),
    "no_build": lambda s: s.__setitem__("build", "."),
    "container_name": lambda s: s.pop("container_name"),
    "entrypoint_cleared": lambda s: s.pop("entrypoint"),
    "exact_command": lambda s: s.__setitem__("command", ["sh", "-c", "x"]),
    "user": lambda s: s.pop("user"),
    "workdir": lambda s: s.pop("working_dir"),
    "read_only": lambda s: s.pop("read_only"),
    "cap_drop_all": lambda s: s.pop("cap_drop"),
    "no_cap_add": lambda s: s.__setitem__("cap_add", ["NET_ADMIN"]),
    "no_new_privileges": lambda s: s.pop("security_opt"),
    "not_privileged": lambda s: s.__setitem__("privileged", True),
    "restart": lambda s: s.pop("restart"),
    "network": lambda s: s.__setitem__("network_mode", "host"),
    "no_volumes": lambda s: s.__setitem__("volumes", ["/:/host"]),
    "ports": lambda s: s.__setitem__("ports", ["3001:3000"]),
    "env_files": lambda s: s.__setitem__("env_file", ["/root/.env"]),
    "identity_env": lambda s: s["environment"].pop("BFX_CHANGE_CLASS"),
    "loader_injection_blanked": lambda s: s["environment"].pop("LD_PRELOAD"),
    "interpreter_injection_blanked": lambda s: s["environment"].pop(
        "NODE_OPTIONS" if "NODE_OPTIONS" in s["environment"] else "PYTHONPATH"),
    "no_host_namespaces": lambda s: s.__setitem__("pid", "host"),
}


@pytest.mark.parametrize("service", ["bot", "webapi", "frontend"])
@pytest.mark.parametrize("prop", PROPERTIES)
def test_dropping_a_property_is_detected(service: str, prop: str) -> None:
    """Discriminating power: each check fails when its property is removed."""
    spec = copy.deepcopy(COMPOSE["services"][service])
    REMOVALS[prop](spec)
    assert not _check(service, spec, prop)


# --------------------------------------------------------------------------- docker compose


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True,
                          check=False).returncode == 0


needs_compose = pytest.mark.skipif(not _compose_available(), reason="docker compose CLI unavailable")


@needs_compose
def test_compose_renders_with_digest_references(tmp_path: Path) -> None:
    shutil.copy(COMPOSE_PATH, tmp_path / "docker-compose.app.yml")
    shutil.copy(ROOT / "deploy/vm/live.env", tmp_path / "live.env")
    rendered = subprocess.run(
        ["docker", "compose", "-f", str(tmp_path / "docker-compose.app.yml"), "config",
         "--no-env-resolution", "--format", "json"],
        capture_output=True, text=True, check=False, env={**_path_env(), **FAKE_ENV},
    )
    assert rendered.returncode == 0, rendered.stderr
    services = json.loads(rendered.stdout)["services"]
    assert services["bot"]["image"] == FAKE_ENV["BFX_BACKEND_IMAGE"]
    assert services["frontend"]["image"] == FAKE_ENV["BFX_FRONTEND_IMAGE"]
    assert services["webapi"]["environment"]["BFX_IMAGE_DIGEST"] == DIGEST_B
    assert services["frontend"]["environment"]["BFX_IMAGE_DIGEST"] == DIGEST_F
    assert services["frontend"]["ports"] == [{
        "mode": "ingress", "host_ip": "127.0.0.1", "target": 3000, "published": "3001",
        "protocol": "tcp"}]


@needs_compose
@pytest.mark.parametrize("missing", sorted(FAKE_ENV))
def test_compose_refuses_to_render_without_each_deploy_variable(tmp_path: Path, missing: str) -> None:
    shutil.copy(COMPOSE_PATH, tmp_path / "docker-compose.app.yml")
    env = {**_path_env(), **{k: v for k, v in FAKE_ENV.items() if k != missing}}
    rendered = subprocess.run(
        ["docker", "compose", "-f", str(tmp_path / "docker-compose.app.yml"), "config",
         "--no-env-resolution"], capture_output=True, text=True, check=False, env=env)
    assert rendered.returncode != 0
    assert missing in rendered.stderr


def _path_env() -> dict[str, str]:
    import os

    return {key: os.environ[key] for key in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT")
            if key in os.environ}


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
def test_compose_created_containers_pass_the_deploy_inspection(tmp_path: Path) -> None:
    """Create (never start) the three services from the real file and inspect them.

    Proves the Compose rendering of entrypoint, user, read-only, capabilities,
    security options, loopback port and blanked injection variables is exactly
    what bfx-deploy's post-up inspection requires. A local image that already has
    a repository digest stands in for both application images; project, network
    and container names are made unique so nothing real is touched.
    """
    image = _local_image_with_repo_digest()
    if image is None:
        pytest.skip("no local image with a repository digest")
    suffix = uuid.uuid4().hex[:10]
    network, project = f"bfx-test-{suffix}", f"bfx-app-test-{suffix}"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    for name in ("bot.env", "webapi.env", "frontend.env"):
        (runtime / name).write_text("EXAMPLE=1\n")
    text = (COMPOSE_TEXT.replace("name: bfx-app", f"name: {project}")
            .replace("/opt/bfx/runtime/", f"{runtime}/")
            .replace("name: bfx_default", f"name: {network}"))
    for container in ("bfx-bot", "bfx-webapi", "bfx-frontend"):
        text = text.replace(f"container_name: {container}", f"container_name: {container}-{suffix}")
    (tmp_path / "docker-compose.app.yml").write_text(text)
    shutil.copy(ROOT / "deploy/vm/live.env", tmp_path / "live.env")
    digest = image.split("@", 1)[1]
    env = {**_path_env(), **FAKE_ENV, "BFX_BACKEND_IMAGE": image, "BFX_FRONTEND_IMAGE": image,
           "BFX_BACKEND_DIGEST": digest, "BFX_FRONTEND_DIGEST": digest}
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
            violations = bfx.container_violations(
                record, service=service, image=image, digest=digest,
                revision=FAKE_ENV["BFX_SOURCE_REVISION"], klass="standard", network=network)
            # Only the names were changed for isolation.
            assert set(violations) <= {"name"}, (service, violations)
    finally:
        subprocess.run([*compose, "down", "--remove-orphans"], capture_output=True, env=env,
                       check=False, timeout=120)
        subprocess.run(["docker", "network", "rm", network], capture_output=True, check=False)
