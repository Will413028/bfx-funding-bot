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
missing secret file stops `up` instead of starting without it. Every other
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

Standard library only; exits 1 and lists every violation.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from typing import Any

SERVICES = ("bot", "webapi", "frontend")
NETWORK = "bfx_default"
CONTAINERS = {"bot": "bfx-bot", "webapi": "bfx-webapi", "frontend": "bfx-frontend"}
COMMANDS = {
    "bot": ["/app/.venv/bin/python", "-m", "bfx_funding_bot.modules.marketfeed.daemon"],
    "webapi": ["/app/.venv/bin/python", "-m", "uvicorn", "bfx_funding_bot.main:app",
               "--host", "0.0.0.0", "--port", "8000"],
    "frontend": ["node", "server.js"],
}
USERS = {"bot": "1000:1000", "webapi": "1000:1000", "frontend": "nextjs"}
PRODUCTION_RUNTIME_DIR = "/opt/bfx/runtime"
RUNTIME_REFERENCE = "${BFX_RUNTIME_DIR:-" + PRODUCTION_RUNTIME_DIR + "}/"
ENV_FILES = {
    "bot": (f"/{RUNTIME_REFERENCE}bot.env", "/live.env"),
    "webapi": (f"/{RUNTIME_REFERENCE}webapi.env",),
    "frontend": (f"/{RUNTIME_REFERENCE}frontend.env",),
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
FRONTEND_PORTS = [{"mode": "ingress", "host_ip": "127.0.0.1", "target": 3000,
                   "published": "3001", "protocol": "tcp"}]


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
        need(not spec.get(key), f"no_{key}")
    networks = spec.get("networks") or {}
    need(set(networks) == {NETWORK}, "network")
    if service == "bot":
        need(((networks.get(NETWORK) or {}).get("aliases") or []) == ["bfx-bot"], "bot_alias")
    need((spec.get("ports") or []) == (FRONTEND_PORTS if service == "frontend" else []), "ports")
    need(all(env.get(key) for key in IDENTITY), "identity_env")
    need(all(env.get(key) == "" for key in LOADER_INJECTION), "loader_injection_blanked")
    if service == "frontend":
        need(env.get("NODE_OPTIONS") == "", "interpreter_injection_blanked")
    else:
        need(all(env.get(key) == "" for key in PYTHON_INJECTION)
             and env.get("PYTHONDONTWRITEBYTECODE") == "1", "interpreter_injection_blanked")
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


def env_file_violations(uninterpolated: Mapping[str, Any]) -> list[str]:
    """Each service's env files, from `config --no-interpolate` (version-stable)."""
    found = []
    services = uninterpolated.get("services") or {}
    for service in SERVICES:
        entries = (services.get(service) or {}).get("env_file") or []
        wanted = ENV_FILES[service]
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
    if set(rendered.get("services") or {}) != set(SERVICES):
        found.append("services")
    if rendered.get("volumes"):
        found.append("volumes")
    network = (rendered.get("networks") or {}).get(NETWORK) or {}
    if set(rendered.get("networks") or {}) != {NETWORK} or network.get("external") is not True \
            or network.get("name") != NETWORK:
        found.append("external_network")
    for service in SERVICES:
        spec = (rendered.get("services") or {}).get(service)
        if isinstance(spec, dict):
            found += service_violations(service, spec)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a rendered VM compose file.")
    parser.add_argument("--kind", choices=("app", "weekly-report"), default="app",
                        help="app: deploy/vm/docker-compose.app.yml (default); weekly-report: "
                             "deploy/vm/ops/docker-compose.weekly-report.yml")
    parser.add_argument("--uninterpolated",
                        help="JSON of `docker compose config --no-interpolate --format json` "
                             "(required for --kind app)")
    args = parser.parse_args(argv)
    if args.kind == "app" and not args.uninterpolated:
        parser.error("--uninterpolated is required for --kind app")
    try:
        rendered = json.load(sys.stdin)
        uninterpolated = None
        if args.kind == "app":
            with open(args.uninterpolated, encoding="utf-8") as stream:
                uninterpolated = json.load(stream)
    except (OSError, ValueError):
        print("compose-policy: input is not readable JSON", file=sys.stderr)
        return 1
    if uninterpolated is None:
        found = weekly_violations(rendered)
    else:
        found = violations(rendered) + env_file_violations(uninterpolated)
    for item in found:
        print(f"compose-policy: violation {item}", file=sys.stderr)
    if not found:
        print("compose-policy: ok")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
