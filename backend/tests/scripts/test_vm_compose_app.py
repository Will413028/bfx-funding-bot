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
SERVE_PATH = ROOT / "deploy/vm/tailscale/serve.json"
DIGEST_B = "sha256:" + "3" * 64
FAKE_ENV = {
    "BFX_BACKEND_IMAGE": f"ghcr.io/will413028/bfx-funding-bot-backend@{DIGEST_B}",
    "BFX_BACKEND_DIGEST": DIGEST_B,
    "BFX_SOURCE_REVISION": "b" * 40,
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


# BFX_TEST_COMPOSE picks another Compose binary (e.g. a standalone 2.38 build) so
# the policy can be checked against more than one Compose version.
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


@pytest.fixture(scope="module")
def runtime_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Empty stand-ins for /opt/bfx/runtime: older Compose stats env files even
    with --no-env-resolution, and CI has no secret directory."""
    directory = tmp_path_factory.mktemp("runtime")
    for name in ("bot.env", "webapi.env", "tailscale.env"):
        (directory / name).write_text("")
    return directory


def _render(runtime: Path, env: dict[str, str] | None = None, *, path: Path = COMPOSE_PATH,
            mode: str = "--no-env-resolution") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*COMPOSE_CMD, "-f", str(path), "config", mode, "--format", "json"],
        capture_output=True, text=True, check=False,
        env={**_path_env(), **(FAKE_ENV if env is None else env), "BFX_RUNTIME_DIR": str(runtime)})


@pytest.fixture(scope="module")
def rendered(runtime_dir: Path) -> dict[str, Any]:
    if not _compose_available():
        pytest.skip("docker compose CLI unavailable")
    result = _render(runtime_dir)
    assert result.returncode == 0, result.stderr
    return dict(json.loads(result.stdout))


def _uninterpolated(runtime: Path, path: Path = COMPOSE_PATH) -> dict[str, Any]:
    result = _render(runtime, path=path, mode="--no-interpolate")
    assert result.returncode == 0, result.stderr
    return dict(json.loads(result.stdout))


@pytest.fixture(scope="module")
def uninterpolated(runtime_dir: Path) -> dict[str, Any]:
    if not _compose_available():
        pytest.skip("docker compose CLI unavailable")
    return _uninterpolated(runtime_dir)


# --------------------------------------------------------------------------- file shape


def test_the_application_services_and_the_ingress_are_defined() -> None:
    assert set(COMPOSE["services"]) == {"bot", "webapi", "netns", "tailscale"}     # no frontend (9d-5)
    assert COMPOSE["name"] == "bfx-app"
    assert COMPOSE["volumes"] == {"bfx_tsstate": {"name": "bfx_tsstate"}}


def test_postgres_and_redis_are_never_part_of_this_project() -> None:
    for word in ("postgres", "redis", "pgdata", "redisdata"):
        assert word not in json.dumps(COMPOSE["services"])


def test_no_image_tag_appears_anywhere_in_the_file() -> None:
    images = {name: spec["image"] for name, spec in COMPOSE["services"].items()}
    assert all(images[name].startswith("${") for name in ("bot", "webapi"))
    assert images["netns"] == images["tailscale"] == policy.TAILSCALE_IMAGE
    assert bfx.THIRD_PARTY_IMAGE.fullmatch(policy.TAILSCALE_IMAGE)
    assert re.search(r"ghcr\.io/\S+:(main|sha-|latest)", COMPOSE_TEXT) is None


@needs_compose
def test_env_files_are_the_required_production_defaults(uninterpolated: dict[str, Any]) -> None:
    assert policy.env_file_violations(uninterpolated) == []
    bot = [entry["path"] for entry in uninterpolated["services"]["bot"]["env_file"]]
    assert bot[0].endswith("/${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/bot.env")
    assert bot[1].endswith("/deploy/vm/live.env")


@needs_compose
@pytest.mark.parametrize(("old", "new", "violation"), [
    ("${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/webapi.env", "${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/bot.env",
     "webapi:env_files"),
    ("${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/bot.env", "${BFX_RUNTIME_DIR:-/tmp}/bot.env", "bot:env_files"),
    ("      - ${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/webapi.env",
     "      - path: ${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/webapi.env\n        required: false",
     "webapi:env_files_required"),
    ("      - live.env\n", "", "bot:env_files"),
])
def test_wrong_or_optional_env_files_are_detected(
    tmp_path: Path, runtime_dir: Path, old: str, new: str, violation: str,
) -> None:
    mutated = tmp_path / "docker-compose.app.yml"
    assert COMPOSE_TEXT.count(old) == 1
    mutated.write_text(COMPOSE_TEXT.replace(old, new))
    shutil.copy(ROOT / "deploy/vm/live.env", tmp_path / "live.env")
    assert violation in policy.env_file_violations(_uninterpolated(runtime_dir, mutated))


def test_policy_deployer_and_backend_agree_on_the_secret_files() -> None:
    """One gid (the files' group = the services' group_add) and one key set (what the
    backend reads from files = what the compose file mounts)."""
    from bfx_funding_bot.core.secret_files import FILE_SECRET_KEYS, SECRETS_DIR_ENV

    assert str(bfx.SECRET_GID) == policy.SECRET_GID
    assert policy.SECRET_KEYS == FILE_SECRET_KEYS
    assert set(policy.SERVICE_SECRETS) == set(bfx.ROLE_CONTAINERS)
    for service in ("bot", "webapi"):
        assert COMPOSE["services"][service]["environment"][SECRETS_DIR_ENV] == policy.SECRETS_DIR
    # The web API never holds the halt token or the vault key (9d-2, 9g).
    assert {"BFX_ADMIN_TOKEN", "BFX_VAULT_KEK"}.isdisjoint(policy.SERVICE_SECRETS["webapi"])


def test_policy_and_deployer_agree_on_names_and_commands() -> None:
    assert {role: policy.CONTAINERS[role] for role in bfx.ROLE_CONTAINERS} == bfx.ROLE_CONTAINERS
    # The netns repair inspects the holder by name; a renamed holder would never be repaired.
    assert policy.INGRESS_CONTAINERS["netns"] == bfx.NETNS_HOLDER
    joiners = {"tailscale": policy.INGRESS_CONTAINERS["tailscale"], "webapi": policy.CONTAINERS["webapi"]}
    assert joiners == bfx.NETNS_JOINERS
    assert set(bfx.ROLE_CONTAINERS) == set(policy.REQUIRED_SERVICES)
    assert tuple(bfx.identity_env(
        bfx.Target("b" * 40, DIGEST_B, "x"), image="x",
        deployment_id="d") or {}) == policy.IDENTITY


def test_retired_change_class_is_gone() -> None:
    assert "BFX_CHANGE_CLASS" not in policy.IDENTITY
    for service in ("bot", "webapi"):
        assert "BFX_CHANGE_CLASS" not in COMPOSE["services"][service]["environment"]


# --------------------------------------------------------------------------- rendered policy


@needs_compose
def test_rendered_compose_file_satisfies_the_policy(rendered: dict[str, Any]) -> None:
    assert policy.violations(rendered) == []


@needs_compose
def test_deployer_reads_the_release_from_the_real_rendering(rendered: dict[str, Any],
                                                           runtime_dir: Path) -> None:
    """bfx-deploy renders the release the same way (--no-env-resolution) and runs what it finds."""
    target = bfx.Target(FAKE_ENV["BFX_SOURCE_REVISION"], DIGEST_B, FAKE_ENV["BFX_BACKEND_IMAGE"])
    if "env_file" not in rendered["services"]["bot"]:
        # Compose 2.38 (CI) folds env files into `environment`: refused, never unchecked.
        with pytest.raises(bfx.DeployError, match="compose_env_files_not_rendered:bot"):
            bfx.release_services(rendered, target=target, runtime_dir=runtime_dir,
                                 release_dir=COMPOSE_PATH.parent)
        return
    services = bfx.release_services(rendered, target=target, runtime_dir=runtime_dir,
                                    release_dir=COMPOSE_PATH.parent)
    assert [(s.name, s.container, s.image, s.joins) for s in services] == [
        ("bot", "bfx-bot", target.backend_image, None),
        ("netns", "bfx-netns", policy.TAILSCALE_IMAGE, None),
        ("tailscale", "bfx-tailscale", policy.TAILSCALE_IMAGE, "netns"),
        ("webapi", "bfx-webapi", target.backend_image, "netns")]
    assert [[p.name for p in s.env_files] for s in services] == [
        ["bot.env", "live.env"], [], ["tailscale.env"], ["webapi.env"]]
    assert all(p.parent in (runtime_dir, COMPOSE_PATH.parent) for s in services for p in s.env_files)
    assert [sorted(p.relative_to(runtime_dir / "secrets").as_posix() for p in s.secret_files)
            for s in services] == [
        [f"bot/{key}" for key in policy.SERVICE_SECRETS["bot"]], [], [],
        [f"webapi/{key}" for key in policy.SERVICE_SECRETS["webapi"]]]
    assert [str(p) for p in bfx.release_config_files(rendered, COMPOSE_PATH.parent)] == [
        "tailscale/serve.json"]


@needs_compose
def test_a_frontend_service_is_refused(rendered: dict[str, Any]) -> None:
    """9d-5: the web API serves the UI; a frontend service coming back is a violation."""
    broken = copy.deepcopy(rendered)
    broken["services"]["frontend"] = copy.deepcopy(broken["services"]["bot"])
    assert "services" in policy.violations(broken)


@needs_compose
@pytest.mark.parametrize(("mutate", "violation"), [
    (lambda r: r["services"].pop("bot"), "services"),
    (lambda r: r["services"].pop("webapi"), "services"),
    (lambda r: r["services"].__setitem__("extra", copy.deepcopy(r["services"]["webapi"])), "services"),
    (lambda r: r["secrets"].__setitem__("key", {"name": "bfx-app_key", "file": "/x"}), "secrets"),
    (lambda r: r["secrets"].pop("webapi_DATABASE_URL"), "secrets"),
    (lambda r: r["secrets"]["bot_BFX_VAULT_KEK"].__setitem__("file", "/etc/kek"),
     "secret_file:bot_BFX_VAULT_KEK"),
    (lambda r: r["secrets"].__setitem__("bot_BFX_VAULT_KEK", {"name": "bfx-app_bot_BFX_VAULT_KEK",
                                                              "environment": "BFX_VAULT_KEK"}),
     "secret_file:bot_BFX_VAULT_KEK"),
    (lambda r: r["secrets"]["webapi_DATABASE_URL"].__setitem__(
        "file", r["secrets"]["bot_DATABASE_URL"]["file"]), "secret_file:webapi_DATABASE_URL"),
    (lambda r: r.__setitem__("configs", {"c": {"name": "bfx-app_c", "content": "x"}}), "configs_file_only"),
    (lambda r: r.__setitem__("configs", {"c": {"name": "bfx-app_c", "environment": "HOME"}}),
     "configs_file_only"),
])
def test_the_service_set_secrets_and_configs_are_enforced(
    rendered: dict[str, Any], mutate: Callable[[dict[str, Any]], None], violation: str,
) -> None:
    broken = copy.deepcopy(rendered)
    mutate(broken)
    assert violation in policy.violations(broken)
    fine = copy.deepcopy(rendered)
    fine["configs"] = {"ts-serve": {"file": "/opt/bfx/releases/r/tailscale/serve.json"}}  # 2.38: no name
    assert policy.violations(fine) == []


def _bind(spec: dict[str, Any]) -> None:
    spec["volumes"].append({"type": "bind", "source": "/etc", "target": "/host"})


INGRESS_MUTATIONS: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("tailscale", lambda s: s.__setitem__("cap_add", ["NET_ADMIN"]), "tailscale:no_cap_add"),
    ("tailscale", lambda s: s.__setitem__("cap_drop", []), "tailscale:cap_drop_all"),
    ("tailscale", lambda s: s.__setitem__("ports", [{"target": 443, "published": "443"}]),
     "tailscale:no_ports"),
    ("tailscale", lambda s: s.__setitem__("devices", ["/dev/net/tun"]), "tailscale:no_devices"),
    ("tailscale", _bind, "tailscale:state_volume_only"),
    ("tailscale", lambda s: s["volumes"].append({"type": "volume", "source": "x", "target": "/x"}),
     "tailscale:state_volume_only"),
    ("tailscale", lambda s: s["configs"][0].__setitem__("target", "/etc/serve.json"),
     "tailscale:serve_config_only"),
    ("tailscale", lambda s: s["environment"].__setitem__("TS_USERSPACE", "false"),
     "tailscale:exact_environment"),
    ("tailscale", lambda s: s["environment"].__setitem__("TS_LOCAL_ADDR_PORT", "[::]:9002"),
     "tailscale:exact_environment"),
    ("tailscale", lambda s: s["environment"].__setitem__("TS_AUTHKEY", "tskey-x"),
     "tailscale:exact_environment"),
    ("tailscale", lambda s: s.__setitem__("network_mode", "host"), "tailscale:joins_netns"),
    ("tailscale", lambda s: s.__setitem__("read_only", False), "tailscale:read_only"),
    ("tailscale", lambda s: s.__setitem__("image", "docker.io/tailscale/tailscale@sha256:" + "d" * 64),
     "tailscale:image_allowlisted_digest"),
    ("tailscale", lambda s: s.__setitem__("command", ["tailscaled"]), "tailscale:image_entrypoint"),
    ("tailscale", lambda s: s.pop("healthcheck"), "tailscale:healthcheck"),
    ("tailscale", lambda s: s.__setitem__("tmpfs", ["/tmp", "/var/run/tailscale", "/var/lib"]),
     "tailscale:tmpfs"),
    ("netns", lambda s: s.__setitem__("entrypoint", ["sh"]), "netns:exact_entrypoint"),
    ("netns", lambda s: s.__setitem__("volumes", [{"type": "volume", "source": "x", "target": "/x"}]),
     "netns:no_volumes"),
    ("netns", lambda s: s.__setitem__("ports", [{"target": 8000, "published": "8000"}]), "netns:no_ports"),
    ("netns", lambda s: s["networks"].__setitem__("other", {}), "netns:network"),
    ("netns", lambda s: s.__setitem__("container_name", "bfx-holder"), "netns:container_name"),
    ("netns", lambda s: s.__setitem__("cap_add", ["NET_ADMIN"]), "netns:no_cap_add"),
    ("netns", lambda s: s.pop("init"), "netns:init"),
    ("netns", lambda s: s["networks"].__setitem__("bfx_default", {"aliases": ["bfx-postgres"]}), "netns:network"),
]


@needs_compose
@pytest.mark.parametrize(("service", "mutate", "violation"), INGRESS_MUTATIONS,
                         ids=[v for _, _, v in INGRESS_MUTATIONS])
def test_the_ingress_shape_is_pinned(
    rendered: dict[str, Any], service: str, mutate: Callable[[dict[str, Any]], None], violation: str,
) -> None:
    broken = copy.deepcopy(rendered)
    mutate(broken["services"][service])
    assert violation in policy.violations(broken)


@needs_compose
@pytest.mark.parametrize(("mutate", "violation"), [
    (lambda r: r["services"].pop("tailscale"), "services"),
    (lambda r: r["services"].pop("netns"), "services"),
    (lambda r: r["volumes"].__setitem__("other", {"name": "other"}), "volumes"),
    (lambda r: r["volumes"]["bfx_tsstate"].pop("name"), "volumes"),
    (lambda r: r["volumes"]["bfx_tsstate"].__setitem__("external", True), "volumes"),
    (lambda r: r["configs"].__setitem__("extra", {"file": "/x/tailscale/serve.json"}), "serve_config"),
    (lambda r: r["configs"]["ts-serve"].__setitem__("file", "/etc/serve.json"), "serve_config"),
    (lambda r: r["configs"]["ts-serve"].__setitem__("name", "other_ts-serve"), "serve_config"),
    (lambda r: r["configs"]["ts-serve"].__setitem__("file", "tailscale/serve.json"), "serve_config"),
    (lambda r: r["configs"].__setitem__("ts-serve", {"name": "bfx-app_ts-serve", "content": "{}"}),
     "configs_file_only"),
    # The web API joins the holder only in 9d-5.
    (lambda r: r["services"]["webapi"].__setitem__("network_mode", "service:tailscale"),
     "webapi:joins_netns"),
])
def test_the_ingress_volume_config_and_join_are_pinned(
    rendered: dict[str, Any], mutate: Callable[[dict[str, Any]], None], violation: str,
) -> None:
    broken = copy.deepcopy(rendered)
    mutate(broken)
    assert violation in policy.violations(broken)


@needs_compose
def test_the_sidecar_reads_only_tailscale_env(uninterpolated: dict[str, Any]) -> None:
    paths = [e["path"] for e in uninterpolated["services"]["tailscale"]["env_file"]]
    assert len(paths) == 1 and paths[0].endswith("/${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/tailscale.env")
    broken = copy.deepcopy(uninterpolated)
    broken["services"]["tailscale"]["env_file"][0]["path"] = paths[0].replace("tailscale.env", "webapi.env")
    assert "tailscale:env_files" in policy.env_file_violations(broken)
    # The holder reads no env file at all.
    broken = copy.deepcopy(uninterpolated)
    broken["services"]["netns"]["env_file"] = [{"path": paths[0], "required": True}]
    assert "netns:env_files" in policy.env_file_violations(broken)


def test_serve_config_proxies_only_to_the_local_web_api() -> None:
    """Read as tracked (Compose never interpolates a config file): one HTTPS listener whose
    only handler proxies to the shared namespace's 127.0.0.1:8000, forwarding the operator
    capability; no Funnel, no host name of this tailnet."""
    text = SERVE_PATH.read_text(encoding="utf-8")
    serve = json.loads(text)
    assert set(serve) == {"TCP", "Web"}
    assert serve["TCP"] == {"443": {"HTTPS": True}}
    assert list(serve["Web"]) == ["${TS_CERT_DOMAIN}:443"]
    assert serve["Web"]["${TS_CERT_DOMAIN}:443"] == {"Handlers": {"/": {
        "Proxy": "http://127.0.0.1:8000",
        "AcceptAppCaps": ["github.com/Will413028/bfx-funding-bot/cap/operator"]}}}
    assert "AllowFunnel" not in text and ".ts.net" not in text


@needs_compose
def test_policy_cli_is_what_ci_runs(
    rendered: dict[str, Any], uninterpolated: dict[str, Any], tmp_path: Path,
) -> None:
    snapshot = tmp_path / "uninterpolated.json"
    snapshot.write_text(json.dumps(uninterpolated))
    argv = [sys.executable, str(ROOT / "deploy/vm/ops/compose_policy.py"), "--uninterpolated", str(snapshot)]
    env = _path_env()
    completed = subprocess.run(argv, input=json.dumps(rendered), capture_output=True, text=True,
                               check=False, env=env)
    assert completed.returncode == 0, completed.stderr
    broken = copy.deepcopy(rendered)
    broken["services"]["bot"]["read_only"] = False
    completed = subprocess.run(argv, input=json.dumps(broken), capture_output=True, text=True,
                               check=False, env=env)
    assert completed.returncode == 1 and "bot:read_only" in completed.stderr
    uninterpolated_broken = copy.deepcopy(uninterpolated)
    uninterpolated_broken["services"]["webapi"]["env_file"][0]["required"] = False
    snapshot.write_text(json.dumps(uninterpolated_broken))
    completed = subprocess.run(argv, input=json.dumps(rendered), capture_output=True, text=True,
                               check=False, env=env)
    assert completed.returncode == 1 and "webapi:env_files_required" in completed.stderr
    # The top-level volume and config are checked on the uninterpolated rendering too.
    uninterpolated_broken = copy.deepcopy(uninterpolated)
    uninterpolated_broken["volumes"]["bfx_tsstate"]["external"] = True
    snapshot.write_text(json.dumps(uninterpolated_broken))
    completed = subprocess.run(argv, input=json.dumps(rendered), capture_output=True, text=True,
                               check=False, env=env)
    assert completed.returncode == 1 and "uninterpolated:volumes" in completed.stderr
    # ... and the secret files' production default (9h).
    uninterpolated_broken = copy.deepcopy(uninterpolated)
    secret = uninterpolated_broken["secrets"]["webapi_DATABASE_URL"]
    secret["file"] = secret["file"].replace("${BFX_RUNTIME_DIR:-/opt/bfx/runtime}", "/srv")
    snapshot.write_text(json.dumps(uninterpolated_broken))
    completed = subprocess.run(argv, input=json.dumps(rendered), capture_output=True, text=True,
                               check=False, env=env)
    assert completed.returncode == 1
    assert "uninterpolated:secret_file:webapi_DATABASE_URL" in completed.stderr


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
    "no_volumes": lambda s: s.__setitem__("volumes", [{"type": "bind", "source": "/", "target": "/host"}]),
    "no_tmpfs": lambda s: s.__setitem__("tmpfs", ["/tmp"]),
    "no_pid": lambda s: s.__setitem__("pid", "host"),
    "ports": lambda s: s.__setitem__("ports", [{"target": 3000, "published": "3001"}]),
    "identity_env": lambda s: s["environment"].pop("BFX_DEPLOYMENT_ID"),
    "loader_injection_blanked": lambda s: s["environment"].pop("LD_PRELOAD"),
    "interpreter_injection_blanked": lambda s: s["environment"].pop("PYTHONPATH"),
    "secrets_dir_env": lambda s: s["environment"].pop("BFX_SECRETS_DIR"),
    "no_secret_env_keys": lambda s: s["environment"].__setitem__("DATABASE_URL", "postgresql://x"),
    "secret_group": lambda s: s.pop("group_add"),
    "exact_secrets": lambda s: s["secrets"].pop(),
}
# The bot keeps its own network; the web API lives in the holder's namespace (9d-5).
BOT_REMOVALS: dict[str, Callable[[dict[str, Any]], None]] = {
    "network": lambda s: s["networks"].__setitem__("other", {}),
    "no_network_mode": lambda s: s.__setitem__("network_mode", "host"),
}
WEBAPI_REMOVALS: dict[str, Callable[[dict[str, Any]], None]] = {
    "no_networks": lambda s: s.__setitem__("networks", {"bfx_default": {}}),
    "joins_netns": lambda s: s.__setitem__("network_mode", "host"),
    "exact_command": lambda s: s.__setitem__("command", [
        "/app/.venv/bin/python", "-m", "uvicorn", "bfx_funding_bot.apps.webapi:app",
        "--host", "0.0.0.0", "--port", "8000"]),
    "ready_healthcheck": lambda s: s.pop("healthcheck"),
}
# Ways to get a secret file wrong that the removals above do not cover.
SECRET_MUTATIONS: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("webapi", lambda s: s["secrets"].append({"source": "bot_BFX_VAULT_KEK", "target": "BFX_VAULT_KEK"}),
     "webapi:exact_secrets"),
    ("webapi", lambda s: s["secrets"].append({"source": "bot_BFX_ADMIN_TOKEN", "target": "BFX_ADMIN_TOKEN"}),
     "webapi:exact_secrets"),
    ("bot", lambda s: s["secrets"][0].__setitem__("target", "/etc/db"), "bot:exact_secrets"),
    ("bot", lambda s: s["secrets"][0].__setitem__("mode", 0o444), "bot:exact_secrets"),
    ("bot", lambda s: s["environment"].__setitem__("BFX_VAULT_KEK", ""), "bot:no_secret_env_keys"),
    ("bot", lambda s: s["environment"].__setitem__("BFX_SECRETS_DIR", "/tmp"), "bot:secrets_dir_env"),
    ("bot", lambda s: s.__setitem__("group_add", ["4600", "0"]), "bot:secret_group"),
    ("webapi", lambda s: s.__setitem__("group_add", ["1000"]), "webapi:secret_group"),
    ("tailscale", lambda s: s.__setitem__("secrets", [{"source": "bot_BFX_VAULT_KEK",
                                                       "target": "BFX_VAULT_KEK"}]),
     "tailscale:no_secrets"),
    ("netns", lambda s: s.__setitem__("group_add", ["4600"]), "netns:no_group_add"),
]


@needs_compose
@pytest.mark.parametrize(("service", "mutate", "violation"), SECRET_MUTATIONS,
                         ids=[f"{v}-{i}" for i, (_, _, v) in enumerate(SECRET_MUTATIONS)])
def test_a_secret_file_mount_out_of_shape_is_a_violation(
    rendered: dict[str, Any], service: str, mutate: Callable[[dict[str, Any]], None], violation: str,
) -> None:
    broken = copy.deepcopy(rendered)
    mutate(broken["services"][service])
    assert violation in policy.violations(broken)


@needs_compose
def test_the_secret_files_default_to_the_production_runtime_dir(
    uninterpolated: dict[str, Any],
) -> None:
    assert policy.secret_violations(uninterpolated, uninterpolated=True) == []
    broken = copy.deepcopy(uninterpolated)
    secret = broken["secrets"]["bot_BFX_VAULT_KEK"]
    secret["file"] = secret["file"].replace("/opt/bfx/runtime", "/home/ubuntu")
    assert policy.secret_violations(broken, uninterpolated=True) == ["secret_file:bot_BFX_VAULT_KEK"]
PROPERTY_CASES = [
    *((service, prop, mutate) for service in ("bot", "webapi") for prop, mutate in sorted(REMOVALS.items())),
    *(("bot", prop, mutate) for prop, mutate in sorted(BOT_REMOVALS.items())),
    *(("webapi", prop, mutate) for prop, mutate in sorted(WEBAPI_REMOVALS.items())),
]


@needs_compose
@pytest.mark.parametrize(("service", "prop", "mutate"), PROPERTY_CASES,
                         ids=[f"{prop}-{service}" for service, prop, _ in PROPERTY_CASES])
def test_dropping_a_property_is_detected_by_name(
    rendered: dict[str, Any], service: str, prop: str, mutate: Callable[[dict[str, Any]], None],
) -> None:
    broken = copy.deepcopy(rendered)
    mutate(broken["services"][service])
    assert f"{service}:{prop}" in policy.violations(broken)


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
@pytest.mark.docker
@needs_compose
def test_compose_created_containers_pass_the_deploy_check(tmp_path: Path) -> None:
    """Create (never start) the services of the real file and check the application ones.

    A local image that already has a repository digest stands in for every image;
    network, volume and container names are made unique so nothing real is touched (so
    only name and project differ from what bfx-deploy expects).
    """
    image = _local_image_with_repo_digest()
    if image is None:
        pytest.skip("no local image with a repository digest")
    suffix = uuid.uuid4().hex[:10]
    network = f"bfx-test-{suffix}"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    for name in ("bot.env", "webapi.env", "tailscale.env"):
        (runtime / name).write_text("EXAMPLE=1\n")
    for service, keys in policy.SERVICE_SECRETS.items():
        (runtime / "secrets" / service).mkdir(parents=True)
        for key in keys:
            (runtime / "secrets" / service / key).write_text("example")
    text = (COMPOSE_TEXT.replace("name: bfx_default", f"name: {network}")
            .replace("name: bfx-app", f"name: bfx-app-test-{suffix}")
            .replace("name: bfx_tsstate", f"name: bfx_tsstate-{suffix}")
            .replace(policy.TAILSCALE_IMAGE, image))
    for container in ("bfx-bot", "bfx-webapi", "bfx-netns", "bfx-tailscale"):
        text = text.replace(f"container_name: {container}", f"container_name: {container}-{suffix}")
    (tmp_path / "docker-compose.app.yml").write_text(text)
    shutil.copy(ROOT / "deploy/vm/live.env", tmp_path / "live.env")
    (tmp_path / "tailscale").mkdir()
    shutil.copy(SERVE_PATH, tmp_path / "tailscale/serve.json")
    digest = image.split("@", 1)[1]
    target = bfx.Target(FAKE_ENV["BFX_SOURCE_REVISION"], digest, image)
    env = {**_path_env(), **FAKE_ENV, "BFX_RUNTIME_DIR": str(runtime),
           "BFX_BACKEND_IMAGE": image, "BFX_BACKEND_DIGEST": digest}
    project = f"bfx-app-test-{suffix}"
    compose = [*COMPOSE_CMD, "-p", project, "-f", str(tmp_path / "docker-compose.app.yml")]
    subprocess.run(["docker", "network", "create", network], check=True, capture_output=True)
    try:
        created = subprocess.run([*compose, "create", "--no-recreate"], capture_output=True,
                                 text=True, check=False, env=env, timeout=120)
        assert created.returncode == 0, created.stderr
        names = [f"{c}-{suffix}" for c in ("bfx-bot", "bfx-webapi")]
        inspected = subprocess.run(["docker", "inspect", *names], capture_output=True, text=True,
                                   check=True)
        for service, record in zip(("bot", "webapi"), json.loads(inspected.stdout), strict=True):
            mismatches = bfx.container_mismatches(
                record, container=bfx.ROLE_CONTAINERS[service], image=image,
                identity=bfx.identity_env(target, image=image,
                                          deployment_id=FAKE_ENV["BFX_DEPLOYMENT_ID"]))
            assert mismatches == ["name", "project"], (service, mismatches)
    finally:
        subprocess.run([*compose, "down", "--remove-orphans", "--volumes"], capture_output=True, env=env,
                       check=False, timeout=120)
        subprocess.run(["docker", "network", "rm", network], capture_output=True, check=False)
