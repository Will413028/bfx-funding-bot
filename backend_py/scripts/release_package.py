"""Trusted host release producer. Docker output is captured, never printed raw.

Host root is the launch authority. The application receives no Docker socket.
This module uses the candidate's shared versioned consumer models and hashes.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from bfx_funding_bot.core.release_identity import (
    LaunchReceipt,
    ReleaseManifest,
    canonical_digest,
    runtime_environment,
)

LAUNCH = ["/app/.venv/bin/python", "-m", "bfx_funding_bot.modules.marketfeed.daemon"]

# Evaluated by the candidate interpreter, using the candidate's shared functions.
MEASURE = """
import json, os, time
from pathlib import Path
from bfx_funding_bot.core.release_identity import measure_inventory, measure_python_inventory, runtime_environment
from bfx_funding_bot.modules.execution.release_worker import RELEASE_SCHEMA_HEAD
from bfx_funding_bot.modules.execution.event_store.writer import DEFAULT_PROJECTOR_VERSION
started = time.perf_counter()
result = dict(inventory=measure_inventory(Path('/app'), require_protected=True),
    python_inventory=measure_python_inventory(Path('/usr/local'), require_protected=True),
    environment=runtime_environment(os.environ), schema_head=RELEASE_SCHEMA_HEAD,
    projector_version=DEFAULT_PROJECTOR_VERSION)
result['measurement_seconds'] = time.perf_counter() - started
print(json.dumps(result))
"""


class PackagingBlocked(RuntimeError):  # noqa: N818
    pass


class Runner(Protocol):
    def __call__(self, args: list[str], *, data: bytes | None = None) -> bytes: ...


def run(args: list[str], *, data: bytes | None = None) -> bytes:
    result = subprocess.run(args, input=data, capture_output=True)
    if result.returncode:
        # Docker errors may contain credentials or an arbitrary environment value.
        raise PackagingBlocked("command_failed:" + args[0])
    return result.stdout


def inspect_image(image: str, platform: str, runner: Runner) -> dict[str, Any]:
    record: dict[str, Any] = json.loads(runner(["docker", "image", "inspect", image]))[0]
    if record["Id"] != image or f'{record["Os"]}/{record["Architecture"]}' != platform:
        raise PackagingBlocked("image_or_platform_mismatch")
    return record


def prepare_backend(*, release_id: str, platform: str, config_file: Path,
                    runner: Runner = run) -> tuple[ReleaseManifest, dict[str, Any]]:
    """Build once from clean tracked HEAD, not caller labels or the working dir.

    Run at repository root. Untracked/ignored host runtime files cannot enter
    git archive. config_file must contain only reviewed nonsecret BFX settings;
    production secrets are loaded only by deployment on the trusted host.
    """
    if runner(["git", "status", "--porcelain", "--untracked-files=no"]).strip():
        raise PackagingBlocked("source_not_clean")
    revision = runner(["git", "rev-parse", "HEAD"]).decode().strip()
    archive = runner(["git", "archive", f"{revision}:backend_py"])
    image = runner(["docker", "build", "--quiet", "--platform", platform,
        "--build-arg", f"GIT_SHA={revision}", "-"], data=archive).decode().strip().splitlines()[-1]
    inspected = inspect_image(image, platform, runner)
    measurement = json.loads(runner(["docker", "run", "--rm", "--pull=never",
        "--read-only", "--network", "none", "--env-file", str(config_file),
        image, "/app/.venv/bin/python", "-c", MEASURE]))
    seconds = measurement.pop("measurement_seconds")
    repo_digests = inspected.get("RepoDigests", [])
    oci_digest = repo_digests[0].split("@", 1)[1] if len(repo_digests) == 1 else None
    manifest = ReleaseManifest(version=1, release_id=release_id, source_revision=revision,
        platform=platform, docker_image_id=image, oci_manifest_digest=oci_digest, **measurement)
    return manifest, {"source_revision": revision,
        "source_archive_sha256": hashlib.sha256(archive).hexdigest(),
        "docker_image_id": image, "measurement_seconds": seconds,
        "manifest_digest": canonical_digest(manifest.model_dump(mode="json"))}


def prepare_frontend(*, revision: str, platform: str, public: dict[str, str],
                     runner: Runner = run) -> dict[str, Any]:
    if set(public) != {"NEXT_PUBLIC_APP_URL", "NEXT_PUBLIC_APP_NAME", "NEXT_PUBLIC_BETTER_AUTH_URL"} or not all(public.values()):
        raise PackagingBlocked("frontend_public_configuration_invalid")
    archive = runner(["git", "archive", f"{revision}:frontend"])
    args = ["docker", "build", "--quiet", "--platform", platform]
    for key, value in sorted(public.items()):
        args += ["--build-arg", f"{key}={value}"]
    image = runner([*args, "-"], data=archive).decode().strip().splitlines()[-1]
    inspect_image(image, platform, runner)
    return {"docker_image_id": image, "platform": platform, "source_revision": revision,
        "public_environment": public, "source_archive_sha256": hashlib.sha256(archive).hexdigest()}


def create_launch(manifest: ReleaseManifest, *, release_dir: Path, env_files: list[Path],
                  network: str, runner: Runner = run,
                  evidence_mounts: dict[str, Path] | None = None,
                  publish: Callable[[LaunchReceipt], None]) -> str:
    """Create, inspect, publish fresh proof, then start ONLY that container.

    Caller must first verify migration/policy/halt receipts and host protection.
    A failed inspection leaves a stopped container for explicit operator cleanup.
    No builds, pulls, volume creation, DB mutation, cancellation or activation.
    """
    evidence_mounts = evidence_mounts or {}
    if any(not re.fullmatch(r"/run/bfx-dr/[a-zA-Z0-9_-]+\.json", dest) for dest in evidence_mounts):
        raise PackagingBlocked("invalid_evidence_mount")
    expected_mounts = {"/run/bfx-release": str(release_dir),
                       **{dest: str(source) for dest, source in evidence_mounts.items()}}
    inspect_image(manifest.docker_image_id, manifest.platform, runner)
    launch_id = uuid4().hex
    hostname = "bfx-" + launch_id
    args = ["docker", "create", "--pull=never", "--platform", manifest.platform,
            "--read-only", "--user", "1000:1000", "--workdir", "/app",
            "--hostname", hostname, "--network", network, "--network-alias", "bfx-bot",
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--mount", f"type=bind,src={release_dir},dst=/run/bfx-release,readonly"]
    for dest, source in evidence_mounts.items():
        args += ["--mount", f"type=bind,src={source},dst={dest},readonly"]
    for env_file in env_files:
        args += ["--env-file", str(env_file)]
    args += ["--env", "BFX_RELEASE_MANIFEST_PATH=/run/bfx-release/manifest.json",
             "--env", "BFX_RELEASE_LAUNCH_RECEIPT_PATH=/run/bfx-release/launch.json",
             manifest.docker_image_id, *LAUNCH]
    container_id = runner(args).decode().strip()
    record = json.loads(runner(["docker", "inspect", container_id]))[0]
    config, host = record["Config"], record["HostConfig"]
    environment = dict(item.split("=", 1) for item in config["Env"])
    mounts = record["Mounts"]
    if (
        record["Id"] != container_id or record["Image"] != manifest.docker_image_id
        or record["Platform"] != "linux" or record["State"]["Running"]
        or config["Hostname"] != hostname or config["User"] != "1000:1000"
        or config["WorkingDir"] != "/app" or config["Cmd"] != LAUNCH
        or config.get("Entrypoint") or not host["ReadonlyRootfs"]
        or host["Privileged"] or host.get("CapAdd") or host["NetworkMode"] != network
        or "ALL" not in (host.get("CapDrop") or [])
        or "no-new-privileges" not in (host.get("SecurityOpt") or [])
        or environment.get("PYTHONDONTWRITEBYTECODE") != "1"
        or any(environment.get(key) for key in
               ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "LD_PRELOAD", "LD_LIBRARY_PATH"))
        or runtime_environment(environment) != manifest.environment
        or len(mounts) != len(expected_mounts) or any(m["RW"] for m in mounts)
        or {m["Destination"]: m["Source"] for m in mounts} != expected_mounts
    ):
        raise PackagingBlocked("inspected_launch_mismatch")
    receipt = LaunchReceipt(version=1, launch_id=launch_id, hostname=hostname,
        container_id=container_id, manifest_digest=canonical_digest(manifest.model_dump(mode="json")),
        docker_image_id=manifest.docker_image_id, platform=manifest.platform)
    publish(receipt)
    runner(["docker", "start", container_id])
    return container_id
