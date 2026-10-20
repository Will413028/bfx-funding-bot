#!/usr/bin/env python3
"""Policies for the VM compose files, checked on what Compose renders.

CI runs (see .github/workflows/ci.yml), with placeholder digests for the deploy
variables and BFX_RUNTIME_DIR pointing at empty stand-ins for the secret files:

    docker compose -f deploy/vm/docker-compose.app.yml config --no-interpolate \\
        --format json > uninterpolated.json
    docker compose -f deploy/vm/docker-compose.app.yml config --no-env-resolution \\
        --format json | python3 deploy/vm/ops/compose_policy.py --uninterpolated uninterpolated.json

Two renderings, because Compose versions differ in one place: an interpolated
`config --format json` lists `env_file` on Compose v5 but folds it into
`environment` on 2.38. The env files are therefore checked only on the
uninterpolated rendering, which every version prints the same way: each service
names exactly its own `${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/<name>.env` (the
production default) plus, for the bot, live.env -- all `required: true`, so a
missing secret file stops `up` instead of starting without it. The bot and the
web API are required and no other application service is allowed (9d-5 removed the
frontend: the web API serves the UI). The tailnet ingress is required too and pinned
to its exact shape: `netns` only holds the network namespace (plan D13), `tailscale`
joins it with the state volume, the Serve config and tailscale.env as its only inputs,
and the web API joins it listening on 127.0.0.1 only, with no network of its own. Top-level `secrets` are
refused, the one top-level volume is the fixed-name node state and the one
config is the tracked serve.json. Every other
hardening property is checked on the interpolated rendering. Checking Compose's
own rendering rather than the YAML text means an anchor, an override or a
changed Compose default cannot slip a property past the check. This replaces
the per-container hardening inspection bfx-deploy used to run on the VM: on the
VM bfx-deploy only confirms each container runs the recorded digest and identity
(bfx_deploy.container_mismatches).

`--kind weekly-report` checks deploy/vm/ops/docker-compose.weekly-report.yml
the same way (CI renders it with placeholder values for the variables the
runner, bfx_weekly_report.py, supplies): one one-shot service on the digest
image with the app's hardening, the reports bind mount as its only writable
host path, a small /tmp tmpfs, no env files (the runner passes exactly the keys
the steps read) and no published ports.

`--kind operator` checks deploy/vm/ops/docker-compose.operator.yml with the
app's two renderings (it has an env file): one one-shot service whose entrypoint
is exactly the image's operator CLI, the app's hardening, no ports, volumes or
tmpfs, exactly the environment keys the wrapper supplies plus the blanked
injection variables (so DATABASE_URL can only come from operator.env, the
bfx_operator role), each wrapper value a required `${VAR:?...}` interpolation,
and exactly one required `${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/operator.env`.
The environment-key check assumes the stand-in operator.env is empty, as in CI:
Compose 2.38 folds env file contents into the interpolated `environment`.

Standard library only; exits 1 and lists every violation.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from typing import Any

# Every app release has the bot and the web API (9d-5 removed the frontend service).
# bfx-deploy derives what it runs from the rendered file (bfx_deploy.release_services).
REQUIRED_SERVICES = ("bot", "webapi")
OPTIONAL_SERVICES: tuple[str, ...] = ()
# The tailnet ingress (9d-4), third-party image, own shape (ingress_violations).
INGRESS_SERVICES = ("netns", "tailscale")
SERVICES = (*REQUIRED_SERVICES, *OPTIONAL_SERVICES)
NETWORK = "bfx_default"
TAILSCALE_IMAGE = ("docker.io/tailscale/tailscale@sha256:"
                   "c507f3a2a6ab1cabd8d809b98edeb41edbd5c3fb6ad9632ffd098b4c7d0b4065")
# bfx_deploy.NETNS_HOLDER inspects this container for the netns repair.
INGRESS_CONTAINERS = {"netns": "bfx-netns", "tailscale": "bfx-tailscale"}
NETNS_ENTRYPOINT = ["sleep", "2147483647"]
TAILSCALE_ENV = {
    "TS_USERSPACE": "true",
    "TS_AUTH_ONCE": "true",
    "TS_HOSTNAME": "bfx-ops",
    "TS_STATE_DIR": "/var/lib/tailscale",
    "TS_SERVE_CONFIG": "/config/serve.json",
    "TS_ENABLE_HEALTH_CHECK": "true",
    "TS_LOCAL_ADDR_PORT": "127.0.0.1:9002",
}
TAILSCALE_TMPFS = ["/tmp", "/var/run/tailscale"]
STATE_VOLUME = "bfx_tsstate"
TAILSCALE_VOLUMES = [{"type": "volume", "source": STATE_VOLUME, "target": "/var/lib/tailscale"}]
SERVE_CONFIG = "ts-serve"
TAILSCALE_CONFIGS = [{"source": SERVE_CONFIG, "target": "/config/serve.json"}]
HEALTHCHECKS = {
    "netns": ["CMD", "true"],
    "tailscale": ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:9002/healthz"],
}
# Keys an ingress service may set at all; anything else (ports, cap_add, devices,
# command, user, extra volumes...) is a violation by name.
INGRESS_KEYS = {
    "netns": frozenset({"image", "pull_policy", "container_name", "entrypoint", "command", "init",
                        "read_only", "cap_drop", "security_opt", "restart", "healthcheck",
                        "networks"}),
    "tailscale": frozenset({"image", "pull_policy", "container_name", "network_mode", "read_only",
                            "cap_drop", "security_opt", "restart", "tmpfs", "env_file",
                            "environment", "volumes", "configs", "healthcheck", "depends_on",
                            "entrypoint", "command"}),
}
CONTAINERS = {"bot": "bfx-bot", "webapi": "bfx-webapi"}
COMMANDS = {
    "bot": ["/app/.venv/bin/python", "-m", "bfx_funding_bot.apps.bot"],
    # Loopback only: inside the holder's namespace, Serve is the one client (9d-5).
    "webapi": ["/app/.venv/bin/python", "-m", "uvicorn", "bfx_funding_bot.apps.webapi:app",
               "--host", "127.0.0.1", "--port", "8000"],
}
USERS = {"bot": "1000:1000", "webapi": "1000:1000"}
WEBAPI_NETWORK_MODE = "service:netns"
WEBAPI_HEALTHCHECK = ["CMD", "/app/.venv/bin/python", "-c",
                      "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen("
                      "'http://127.0.0.1:8000/ready',timeout=5).status==200 else 1)"]
PRODUCTION_RUNTIME_DIR = "/opt/bfx/runtime"
RUNTIME_REFERENCE = "${BFX_RUNTIME_DIR:-" + PRODUCTION_RUNTIME_DIR + "}/"
ENV_FILES = {
    "bot": (f"/{RUNTIME_REFERENCE}bot.env", "/live.env"),
    "webapi": (f"/{RUNTIME_REFERENCE}webapi.env",),
    "tailscale": (f"/{RUNTIME_REFERENCE}tailscale.env",),
    "netns": (),
}
IDENTITY = ("BFX_IMAGE_DIGEST", "BFX_SOURCE_REVISION", "BFX_DEPLOYMENT_ID")
LOADER_INJECTION = ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT")
PYTHON_INJECTION = ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE")
FORBIDDEN_KEYS = ("build", "cap_add", "privileged", "volumes", "tmpfs", "devices", "pid", "ipc",
                  "userns_mode", "cgroup", "network_mode", "expose", "extra_hosts", "sysctls")
WEEKLY_PROJECT = "bfx-weekly-report"
WEEKLY_SERVICE = "weekly-report"
WEEKLY_ENV = ("DATABASE_URL", "BFX_EXCHANGE_ACCOUNT_ID", "BFX_DEPLOYMENT_ENV",
              "BFX_IMAGE_DIGEST", "BFX_SOURCE_REVISION")
WEEKLY_TMPFS = ["/tmp:rw,noexec,nosuid,size=64m"]
WEEKLY_REPORTS = {"type": "bind", "source": "/home/ubuntu/bfx/reports", "target": "/reports"}
WEEKLY_FORBIDDEN_KEYS = (*(k for k in FORBIDDEN_KEYS if k not in ("volumes", "tmpfs")),
                         "container_name", "env_file", "ports", "healthcheck")
OPERATOR_PROJECT = "bfx-operator"
OPERATOR_SERVICE = "operator"
OPERATOR_ENTRYPOINT = ["/app/.venv/bin/python", "-m", "bfx_funding_bot.apps.operator_cli"]
# environment key -> the variable the wrapper must supply (a `${VAR:?...}` reference)
OPERATOR_INTERPOLATED = {
    "BFX_OPERATOR_TAILNET_IDENTITY": "BFX_OPERATOR_TAILNET_IDENTITY",
    "BFX_DEPLOYMENT_ENV": "BFX_DEPLOYMENT_ENV",
    "BFX_IMAGE_DIGEST": "BFX_BACKEND_DIGEST",
    "BFX_SOURCE_REVISION": "BFX_SOURCE_REVISION",
}
OPERATOR_ENV_KEYS = frozenset({*OPERATOR_INTERPOLATED, "PYTHONDONTWRITEBYTECODE",
                               *LOADER_INJECTION, *PYTHON_INJECTION})
OPERATOR_ENV_FILES = {OPERATOR_SERVICE: (f"/{RUNTIME_REFERENCE}operator.env",)}
OPERATOR_FORBIDDEN_KEYS = (*FORBIDDEN_KEYS, "container_name", "ports", "command", "healthcheck")


def service_violations(service: str, spec: Mapping[str, Any]) -> list[str]:
    env = spec.get("environment") or {}
    found = []

    def need(ok: bool, name: str) -> None:
        if not ok:
            found.append(f"{service}:{name}")

    image = str(spec.get("image", ""))
    need("@sha256:" in image and ":main" not in image, "image_digest_reference")
    need(spec.get("pull_policy") == "never", "pull_policy_never")
    need(spec.get("container_name") == CONTAINERS[service], "container_name")
    need(spec.get("entrypoint") == [], "entrypoint_cleared")
    need(spec.get("command") == COMMANDS[service], "exact_command")
    need(spec.get("user") == USERS[service], "user")
    need(spec.get("working_dir") == "/app", "workdir")
    need(spec.get("read_only") is True, "read_only")
    need(spec.get("cap_drop") == ["ALL"], "cap_drop_all")
    need(spec.get("security_opt") == ["no-new-privileges:true"], "no_new_privileges")
    need(spec.get("restart") == "unless-stopped", "restart")
    for key in FORBIDDEN_KEYS:
        if not (service == "webapi" and key == "network_mode"):
            need(not spec.get(key), f"no_{key}")
    networks = spec.get("networks") or {}
    if service == "webapi":
        need(spec.get("network_mode") == WEBAPI_NETWORK_MODE, "joins_netns")
        need(not networks, "no_networks")
        check = spec.get("healthcheck") or {}
        need(check.get("test") == WEBAPI_HEALTHCHECK and not check.get("disable"), "ready_healthcheck")
    else:
        need(set(networks) == {NETWORK}, "network")
        need(((networks.get(NETWORK) or {}).get("aliases") or []) == ["bfx-bot"], "bot_alias")
    need(not spec.get("ports"), "ports")
    need(all(env.get(key) for key in IDENTITY), "identity_env")
    need(all(env.get(key) == "" for key in LOADER_INJECTION), "loader_injection_blanked")
    need(all(env.get(key) == "" for key in PYTHON_INJECTION)
         and env.get("PYTHONDONTWRITEBYTECODE") == "1", "interpreter_injection_blanked")
    return found


def ingress_violations(service: str, spec: Mapping[str, Any]) -> list[str]:
    """The holder and the sidecar, each pinned to exactly one shape."""
    found = []

    def need(ok: bool, name: str) -> None:
        if not ok:
            found.append(f"{service}:{name}")

    need(spec.get("image") == TAILSCALE_IMAGE, "image_allowlisted_digest")
    need(spec.get("pull_policy") == "never", "pull_policy_never")
    need(spec.get("container_name") == INGRESS_CONTAINERS[service], "container_name")
    need(spec.get("read_only") is True, "read_only")
    need(spec.get("cap_drop") == ["ALL"], "cap_drop_all")
    need(spec.get("security_opt") == ["no-new-privileges:true"], "no_new_privileges")
    need(spec.get("restart") == "unless-stopped", "restart")
    check = spec.get("healthcheck") or {}
    need(check.get("test") == HEALTHCHECKS[service] and not check.get("disable"), "healthcheck")
    for key in sorted(set(spec) - INGRESS_KEYS[service]):
        need(not spec.get(key), f"no_{key}")
    if service == "netns":
        need(spec.get("entrypoint") == NETNS_ENTRYPOINT and not spec.get("command"), "exact_entrypoint")
        need(spec.get("init") is True, "init")
        networks = spec.get("networks") or {}
        need(set(networks) == {NETWORK} and not networks.get(NETWORK), "network")
    else:
        # The image's own entrypoint (containerboot), nothing appended.
        need(not spec.get("entrypoint") and not spec.get("command"), "image_entrypoint")
        need(spec.get("network_mode") == "service:netns", "joins_netns")
        need(spec.get("environment") == TAILSCALE_ENV, "exact_environment")
        need(spec.get("tmpfs") == TAILSCALE_TMPFS, "tmpfs")
        volumes = [{k: v for k, v in m.items() if k != "volume" or v}
                   if isinstance(m, dict) else m for m in spec.get("volumes") or []]
        need(volumes == TAILSCALE_VOLUMES, "state_volume_only")
        need(spec.get("configs") == TAILSCALE_CONFIGS, "serve_config_only")
    return found


def reports_mount_ok(mount: object) -> bool:
    """Exactly the reports dir -> /reports, a writable bind, never created by Docker.

    Compose versions render `create_host_path: false` differently: v5 prints it
    (`"bind": {"create_host_path": false}`), 2.38 omits a false value and prints
    `"bind": {}`. An absent value therefore cannot be read as either answer, so
    only an explicit `true` is refused here; the file itself pins `false`
    (tests) and bfx_weekly_report.py refuses to run when the directory is
    missing, so nothing is ever created whichever Compose runs it. Any other
    key -- read_only true, propagation, SELinux relabel, a volume or tmpfs
    option -- is a violation.
    """
    if not isinstance(mount, dict):
        return False
    if {k: mount.get(k) for k in WEEKLY_REPORTS} != WEEKLY_REPORTS:
        return False
    if set(mount) - {*WEEKLY_REPORTS, "bind", "read_only"} or mount.get("read_only", False) is not False:
        return False
    bind = mount.get("bind", {})
    return isinstance(bind, dict) and not set(bind) - {"create_host_path"} \
        and bind.get("create_host_path", False) is False


def weekly_violations(rendered: Mapping[str, Any]) -> list[str]:
    """The weekly-report job file (deploy/vm/ops/docker-compose.weekly-report.yml)."""
    found = []
    if rendered.get("name") != WEEKLY_PROJECT:
        found.append("project_name")
    services = rendered.get("services") or {}
    if set(services) != {WEEKLY_SERVICE}:
        found.append("services")
    if rendered.get("volumes"):
        found.append("volumes")
    network = (rendered.get("networks") or {}).get(NETWORK) or {}
    if set(rendered.get("networks") or {}) != {NETWORK} or network.get("external") is not True \
            or network.get("name") != NETWORK:
        found.append("external_network")
    spec = services.get(WEEKLY_SERVICE)
    if not isinstance(spec, dict):
        return found
    env = spec.get("environment") or {}

    def need(ok: bool, name: str) -> None:
        if not ok:
            found.append(f"{WEEKLY_SERVICE}:{name}")

    image = str(spec.get("image", ""))
    need("@sha256:" in image and ":main" not in image, "image_digest_reference")
    need(spec.get("pull_policy") == "never", "pull_policy_never")
    need(spec.get("entrypoint") == [], "entrypoint_cleared")
    command = spec.get("command")
    need(isinstance(command, list) and len(command) == 3 and command[:2] == ["sh", "-c"],
         "shell_command")
    need(spec.get("user") == "1000:1000", "user")
    need(spec.get("working_dir") == "/app", "workdir")
    need(spec.get("read_only") is True, "read_only")
    need(spec.get("cap_drop") == ["ALL"], "cap_drop_all")
    need(spec.get("security_opt") == ["no-new-privileges:true"], "no_new_privileges")
    need(spec.get("restart") == "no", "restart")
    for key in WEEKLY_FORBIDDEN_KEYS:
        need(not spec.get(key), f"no_{key}")
    need(spec.get("tmpfs") == WEEKLY_TMPFS, "tmpfs")
    volumes = spec.get("volumes") or []
    need(len(volumes) == 1 and reports_mount_ok(volumes[0]), "reports_mount")
    need(set(spec.get("networks") or {}) == {NETWORK}, "network")
    need(all(env.get(key) for key in WEEKLY_ENV), "runtime_env")
    need(all(env.get(key) == "" for key in LOADER_INJECTION), "loader_injection_blanked")
    need(all(env.get(key) == "" for key in PYTHON_INJECTION)
         and env.get("PYTHONDONTWRITEBYTECODE") == "1", "interpreter_injection_blanked")
    return found


def operator_violations(rendered: Mapping[str, Any]) -> list[str]:
    """The operator CLI file (deploy/vm/ops/docker-compose.operator.yml), interpolated."""
    found = []
    if rendered.get("name") != OPERATOR_PROJECT:
        found.append("project_name")
    services = rendered.get("services") or {}
    if set(services) != {OPERATOR_SERVICE}:
        found.append("services")
    if rendered.get("volumes"):
        found.append("volumes")
    network = (rendered.get("networks") or {}).get(NETWORK) or {}
    if set(rendered.get("networks") or {}) != {NETWORK} or network.get("external") is not True \
            or network.get("name") != NETWORK:
        found.append("external_network")
    spec = services.get(OPERATOR_SERVICE)
    if not isinstance(spec, dict):
        return found
    env = spec.get("environment") or {}

    def need(ok: bool, name: str) -> None:
        if not ok:
            found.append(f"{OPERATOR_SERVICE}:{name}")

    image = str(spec.get("image", ""))
    need("@sha256:" in image and ":main" not in image, "image_digest_reference")
    need(spec.get("pull_policy") == "never", "pull_policy_never")
    need(spec.get("entrypoint") == OPERATOR_ENTRYPOINT, "exact_entrypoint")
    need(spec.get("user") == "1000:1000", "user")
    need(spec.get("working_dir") == "/app", "workdir")
    need(spec.get("read_only") is True, "read_only")
    need(spec.get("cap_drop") == ["ALL"], "cap_drop_all")
    need(spec.get("security_opt") == ["no-new-privileges:true"], "no_new_privileges")
    need(spec.get("restart") == "no", "restart")
    for key in OPERATOR_FORBIDDEN_KEYS:
        need(not spec.get(key), f"no_{key}")
    need(set(spec.get("networks") or {}) == {NETWORK}, "network")
    need(set(env) == OPERATOR_ENV_KEYS, "environment_keys")
    need(all(env.get(key) for key in OPERATOR_INTERPOLATED), "runtime_env")
    need(all(env.get(key) == "" for key in LOADER_INJECTION), "loader_injection_blanked")
    need(all(env.get(key) == "" for key in PYTHON_INJECTION)
         and env.get("PYTHONDONTWRITEBYTECODE") == "1", "interpreter_injection_blanked")
    return found


def operator_uninterpolated_violations(uninterpolated: Mapping[str, Any]) -> list[str]:
    """The operator file's env file and required wrapper variables, from `--no-interpolate`."""
    found = env_file_violations(uninterpolated, OPERATOR_ENV_FILES, optional=())
    spec = (uninterpolated.get("services") or {}).get(OPERATOR_SERVICE) or {}
    env = spec.get("environment") or {}
    if not all(str(env.get(key, "")).startswith("${" + variable + ":?")
               for key, variable in OPERATOR_INTERPOLATED.items()):
        found.append(f"{OPERATOR_SERVICE}:required_interpolation")
    return found


def env_file_violations(
    uninterpolated: Mapping[str, Any],
    wanted_files: Mapping[str, tuple[str, ...]] = ENV_FILES,
    optional: tuple[str, ...] = OPTIONAL_SERVICES,
) -> list[str]:
    """Each service's env files, from `config --no-interpolate` (version-stable).

    `wanted_files` maps each service to the path suffixes of its env files, in
    order; the default is the app file's. A service in `optional` that the file
    does not define is skipped (whether it may be absent is violations()'s call).
    """
    found = []
    services = uninterpolated.get("services") or {}
    for service, wanted in wanted_files.items():
        if service in optional and service not in services:
            continue
        entries = (services.get(service) or {}).get("env_file") or []
        paths = [str(e.get("path", "")) if isinstance(e, dict) else "" for e in entries]
        if len(paths) != len(wanted) or not all(
                path.endswith(end) for path, end in zip(paths, wanted, strict=False)):
            found.append(f"{service}:env_files")
        if any(not isinstance(e, dict) or e.get("required") is not True for e in entries):
            found.append(f"{service}:env_files_required")
    return found


def violations(rendered: Mapping[str, Any]) -> list[str]:
    found = []
    if rendered.get("name") != "bfx-app":
        found.append("project_name")
    names = set(rendered.get("services") or {})
    if not {*REQUIRED_SERVICES, *INGRESS_SERVICES} <= names <= {*SERVICES, *INGRESS_SERVICES}:
        found.append("services")
    found += top_level_violations(rendered)
    network = (rendered.get("networks") or {}).get(NETWORK) or {}
    if set(rendered.get("networks") or {}) != {NETWORK} or network.get("external") is not True \
            or network.get("name") != NETWORK:
        found.append("external_network")
    for service in SERVICES:
        spec = (rendered.get("services") or {}).get(service)
        if isinstance(spec, dict):
            found += service_violations(service, spec)
    for service in INGRESS_SERVICES:
        spec = (rendered.get("services") or {}).get(service)
        if isinstance(spec, dict):
            found += ingress_violations(service, spec)
    return found


def top_level_violations(rendered: Mapping[str, Any]) -> list[str]:
    """Top-level volumes, secrets and configs; checked on both renderings."""
    found = []
    # The node state, under a fixed name so a renamed project never registers a new node.
    if rendered.get("volumes") != {STATE_VOLUME: {"name": STATE_VOLUME}}:
        found.append("volumes")
    # Nothing writes or rotates a Compose secret; a config is a tracked file that
    # bfx-deploy writes from the release's own revision, never an interpolated
    # `content`/`environment` value.
    if rendered.get("secrets"):
        found.append("no_secrets")
    configs = rendered.get("configs") or {}
    if not isinstance(configs, dict) or any(
            not isinstance(c, dict) or set(c) - {"name", "file"} or not c.get("file")
            for c in configs.values()):
        found.append("configs_file_only")
    serve = configs.get(SERVE_CONFIG) if isinstance(configs, dict) else None
    serve_file = str(serve.get("file", "")) if isinstance(serve, dict) else ""
    if set(configs) != {SERVE_CONFIG} or not isinstance(serve, dict) \
            or not serve_file.startswith("/") or not serve_file.endswith("/tailscale/serve.json") \
            or serve.get("name", f"bfx-app_{SERVE_CONFIG}") != f"bfx-app_{SERVE_CONFIG}":
        found.append("serve_config")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a rendered VM compose file.")
    parser.add_argument("--kind", choices=("app", "weekly-report", "operator"), default="app",
                        help="app: deploy/vm/docker-compose.app.yml (default); weekly-report: "
                             "deploy/vm/ops/docker-compose.weekly-report.yml; operator: "
                             "deploy/vm/ops/docker-compose.operator.yml")
    parser.add_argument("--uninterpolated",
                        help="JSON of `docker compose config --no-interpolate --format json` "
                             "(required for --kind app and --kind operator)")
    args = parser.parse_args(argv)
    if args.kind in ("app", "operator") and not args.uninterpolated:
        parser.error(f"--uninterpolated is required for --kind {args.kind}")
    try:
        rendered = json.load(sys.stdin)
        uninterpolated = None
        if args.kind in ("app", "operator"):
            with open(args.uninterpolated, encoding="utf-8") as stream:
                uninterpolated = json.load(stream)
    except (OSError, ValueError):
        print("compose-policy: input is not readable JSON", file=sys.stderr)
        return 1
    if uninterpolated is None:
        found = weekly_violations(rendered)
    elif args.kind == "operator":
        found = operator_violations(rendered) + operator_uninterpolated_violations(uninterpolated)
    else:
        found = violations(rendered) + env_file_violations(uninterpolated) \
            + [f"uninterpolated:{v}" for v in top_level_violations(uninterpolated)]
    for item in found:
        print(f"compose-policy: violation {item}", file=sys.stderr)
    if not found:
        print("compose-policy: ok")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
