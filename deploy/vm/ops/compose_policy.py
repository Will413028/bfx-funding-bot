#!/usr/bin/env python3
"""Policy for deploy/vm/docker-compose.app.yml, checked on what Compose renders.

CI runs (see .github/workflows/ci.yml):

    docker compose -f deploy/vm/docker-compose.app.yml config \\
        --no-env-resolution --format json | python3 deploy/vm/ops/compose_policy.py

with placeholder digests for the deploy variables. Checking Compose's own
rendering rather than the YAML text means an anchor, an override or a changed
Compose default cannot slip a property past the check. This replaces the
per-container hardening inspection bfx-deploy used to run on the VM: the file
that creates the containers is the one reviewed and checked here, and on the
VM bfx-deploy only confirms each container runs the recorded digest and
identity (bfx_deploy.container_mismatches).

Standard library only; exits 1 and lists every violation.
"""

from __future__ import annotations

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
ENV_FILES = {
    "bot": ["/opt/bfx/runtime/bot.env", "live.env"],
    "webapi": ["/opt/bfx/runtime/webapi.env"],
    "frontend": ["/opt/bfx/runtime/frontend.env"],
}
IDENTITY = ("BFX_IMAGE_DIGEST", "BFX_SOURCE_REVISION", "BFX_CHANGE_CLASS", "BFX_DEPLOYMENT_ID")
LOADER_INJECTION = ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT")
PYTHON_INJECTION = ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE")
FORBIDDEN_KEYS = ("build", "cap_add", "privileged", "volumes", "tmpfs", "devices", "pid", "ipc",
                  "userns_mode", "cgroup", "network_mode", "expose", "extra_hosts", "sysctls")
FRONTEND_PORTS = [{"mode": "ingress", "host_ip": "127.0.0.1", "target": 3000,
                   "published": "3001", "protocol": "tcp"}]


def _env_files(spec: Mapping[str, Any]) -> list[str]:
    files = []
    for entry in spec.get("env_file") or []:
        path = entry.get("path") if isinstance(entry, dict) else entry
        files.append(str(path))
    return files


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
    files = _env_files(spec)
    wanted = ENV_FILES[service]
    need(len(files) == len(wanted) and all(
        f == w if w.startswith("/") else f.endswith("/" + w) for f, w in zip(files, wanted, strict=False)
    ), "env_files")
    need(all(env.get(key) for key in IDENTITY), "identity_env")
    need(all(env.get(key) == "" for key in LOADER_INJECTION), "loader_injection_blanked")
    if service == "frontend":
        need(env.get("NODE_OPTIONS") == "", "interpreter_injection_blanked")
    else:
        need(all(env.get(key) == "" for key in PYTHON_INJECTION)
             and env.get("PYTHONDONTWRITEBYTECODE") == "1", "interpreter_injection_blanked")
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


def main() -> int:
    try:
        rendered = json.load(sys.stdin)
    except ValueError:
        print("compose-policy: input is not JSON", file=sys.stderr)
        return 1
    found = violations(rendered)
    for item in found:
        print(f"compose-policy: violation {item}", file=sys.stderr)
    if not found:
        print("compose-policy: ok")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
