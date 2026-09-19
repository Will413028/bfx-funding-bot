"""Trusted host release producer. Docker output is captured, never printed raw.

Host root is the launch authority. The application receives no Docker socket.
This module uses the candidate's shared versioned consumer models and hashes.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from bfx_funding_bot.core.release_identity import (
    LaunchReceipt,
    PackagedImageIdentity,
    ReleaseManifest,
    canonical_digest,
    identity_json,
    runtime_environment,
)
from scripts.image_artifact import PackagingBlocked, Runner, inspect_archive, resolve_image, run

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


def read_env(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in result:
            raise PackagingBlocked("invalid_or_duplicate_environment_key")
        result[key] = value
    return result


def run_one_shot(identity: PackagedImageIdentity, *, env: Path, network: str,
                 command: list[str], extra_env: dict[str, str] | None = None,
                 mounts: list[str] | None = None, tmpfs: bool = True,
                 runner: Runner = run) -> bytes:
    """Create/inspect before any execution; capture output and always remove ours.

    No auto-remove: retain the stopped container long enough to check its exit
    status, then remove only the validated create result (and anonymous volumes,
    as docker run --rm did). Never retry a command after any failure.
    """
    expected_env = {**read_env(env), **(extra_env or {})}
    expected_mounts = {}
    for mount in mounts or []:
        fields = mount.split(",")
        parts = dict(field.split("=", 1) for field in fields if "=" in field)
        if (len(fields) != 4 or set(parts) != {"type", "src", "dst"}
            or parts["type"] != "bind" or "readonly" not in fields
            or not Path(parts["src"]).is_absolute() or not Path(parts["dst"]).is_absolute()
            or parts["dst"] in expected_mounts):
            raise PackagingBlocked("invalid_one_shot_mount")
        expected_mounts[parts["dst"]] = parts["src"]
    image = resolve_image(identity, runner)
    expected_tmpfs = {"/tmp": "rw,noexec,nosuid,size=64m"} if tmpfs else {}
    args = ["docker", "create", "--pull=never", "--platform", identity.platform,
        "--read-only", "--user", "1000:1000", "--entrypoint", "", "--workdir", "/app",
        "--network", network, "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--env-file", str(env)]
    for key, value in expected_tmpfs.items():
        args += ["--tmpfs", f"{key}:{value}"]
    for key, value in (extra_env or {}).items():
        args += ["--env", f"{key}={value}"]
    for mount in mounts or []:
        args += ["--mount", mount]
    cid = runner([*args, image, *command]).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", cid):
        raise PackagingBlocked("invalid_created_container_id")
    try:
        def inspect() -> dict[str, Any]:
            records = identity_json(runner(["docker", "inspect", cid]))
            if not isinstance(records, list) or len(records) != 1:
                raise PackagingBlocked("one_shot_inspection_invalid")
            record: dict[str, Any] = records[0]
            if record["Id"] != cid or record["Image"] != image or record["Platform"] != "linux":
                raise PackagingBlocked("one_shot_identity_mismatch")
            return record

        record = inspect()
        config, host = record["Config"], record["HostConfig"]
        observed_env = dict(item.split("=", 1) for item in config["Env"])
        observed_mounts = record["Mounts"]
        if (record["State"]["Running"] or record["State"]["Status"] != "created"
            or config["Cmd"] != command or config.get("Entrypoint")
            or config["User"] != "1000:1000" or config["WorkingDir"] != "/app"
            or not host["ReadonlyRootfs"] or host["Privileged"] or host.get("CapAdd")
            or "ALL" not in (host.get("CapDrop") or [])
            or "no-new-privileges" not in (host.get("SecurityOpt") or [])
            or host["NetworkMode"] != network or (host.get("Tmpfs") or {}) != expected_tmpfs
            or any(observed_env.get(k) != v for k, v in expected_env.items())
            or observed_env.get("PYTHONDONTWRITEBYTECODE") != "1"
            or any(observed_env.get(k) for k in
                   ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "LD_PRELOAD", "LD_LIBRARY_PATH"))
            or len(observed_mounts) != len(expected_mounts)
            or any(m["Type"] != "bind" or m["RW"] for m in observed_mounts)
            or {m["Destination"]: m["Source"] for m in observed_mounts} != expected_mounts):
            raise PackagingBlocked("one_shot_settings_mismatch")
        output = b""
        start_error = None
        try:
            output = runner(["docker", "start", "--attach", cid])
        except PackagingBlocked as exc:
            start_error = exc
        state = inspect()["State"]
        if state["Running"] or state["Status"] != "exited" or type(state["ExitCode"]) is not int:
            raise PackagingBlocked("one_shot_exit_unproven")
        if state["ExitCode"] != 0:
            raise PackagingBlocked(f"one_shot_exit_nonzero:{state['ExitCode']}")
        if start_error is not None:
            raise start_error
        return output
    except (ValueError, KeyError, TypeError, IndexError):
        raise PackagingBlocked("one_shot_inspection_invalid") from None
    finally:
        runner(["docker", "rm", "--force", "--volumes", cid])


def source_archive(revision: str, component: str, runner: Runner = run) -> bytes:
    root = runner(["git", "rev-parse", "--show-toplevel"]).decode().strip()
    timestamp = runner(["git", "-C", root, "show", "-s", "--format=%ct", revision]).decode().strip()
    return runner(["git", "-C", root, "archive", "--format=tar", f"--mtime=@{timestamp}",
                   f"{revision}:{component}"])


def inspect_image(image: str, platform: str, runner: Runner) -> dict[str, Any]:
    """Check a build result before exporting; never a deployment authority."""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise PackagingBlocked("immutable_build_id_required")
    record: dict[str, Any] = json.loads(runner(["docker", "image", "inspect", image]))[0]
    if record["Id"] != image or f'{record["Os"]}/{record["Architecture"]}' != platform:
        raise PackagingBlocked("image_or_platform_mismatch")
    return record


def export_image(image: str, platform: str, path: Path, runner: Runner) -> dict[str, Any]:
    if path.exists() or path.is_symlink():
        raise PackagingBlocked("new_archive_path_required")
    inspect_image(image, platform, runner)
    runner(["docker", "save", "--output", str(path), image])
    identity = inspect_archive(path)
    if identity.platform != platform or resolve_image(identity, runner) != image:
        raise PackagingBlocked("exported_image_mismatch")
    with path.open("rb") as stream:
        archive_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    path.chmod(0o444)
    return {"image": identity.model_dump(mode="json"), "archive_filename": path.name,
            "archive_sha256": archive_digest}


def prepare_backend(*, release_id: str, platform: str, config_file: Path, archive_path: Path,
                    runner: Runner = run) -> tuple[ReleaseManifest, dict[str, Any]]:
    """Build once from clean tracked HEAD, not caller labels or the working dir.

    Resolve the repository root. Untracked/ignored host runtime files cannot enter
    git archive. config_file must contain only reviewed nonsecret BFX settings;
    production secrets are loaded only by deployment on the trusted host.
    """
    root = runner(["git", "rev-parse", "--show-toplevel"]).decode().strip()
    if runner(["git", "-C", root, "status", "--porcelain", "--untracked-files=no"]).strip():
        raise PackagingBlocked("source_not_clean")
    revision = runner(["git", "-C", root, "rev-parse", "HEAD"]).decode().strip()
    archive = source_archive(revision, "backend_py", runner)
    image = runner(["docker", "build", "--quiet", "--platform", platform,
        "--build-arg", f"GIT_SHA={revision}", "-"], data=archive).decode().strip().splitlines()[-1]
    artifact = export_image(image, platform, archive_path, runner)
    measurement = json.loads(run_one_shot(PackagedImageIdentity.model_validate(artifact["image"]),
        env=config_file, network="none", tmpfs=False,
        command=["/app/.venv/bin/python", "-c", MEASURE], runner=runner))
    seconds = measurement.pop("measurement_seconds")
    manifest = ReleaseManifest(version=2, release_id=release_id, source_revision=revision,
        image=artifact["image"], **measurement)
    return manifest, {"source_revision": revision,
        "source_archive_sha256": hashlib.sha256(archive).hexdigest(),
        **artifact, "measurement_seconds": seconds,
        "manifest_digest": canonical_digest(manifest.model_dump(mode="json"))}


def prepare_frontend(*, revision: str, platform: str, public: dict[str, str], archive_path: Path,
                     runner: Runner = run) -> dict[str, Any]:
    if set(public) != {"NEXT_PUBLIC_APP_URL", "NEXT_PUBLIC_APP_NAME", "NEXT_PUBLIC_BETTER_AUTH_URL"} or not all(public.values()):
        raise PackagingBlocked("frontend_public_configuration_invalid")
    archive = source_archive(revision, "frontend", runner)
    args = ["docker", "build", "--quiet", "--platform", platform]
    for key, value in sorted(public.items()):
        args += ["--build-arg", f"{key}={value}"]
    image = runner([*args, "-"], data=archive).decode().strip().splitlines()[-1]
    artifact = export_image(image, platform, archive_path, runner)
    return {**artifact, "source_revision": revision,
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
    image = resolve_image(manifest.image, runner)
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
             image, *LAUNCH]
    container_id = runner(args).decode().strip()
    record = json.loads(runner(["docker", "inspect", container_id]))[0]
    config, host = record["Config"], record["HostConfig"]
    environment = dict(item.split("=", 1) for item in config["Env"])
    mounts = record["Mounts"]
    if (
        record["Id"] != container_id or record["Image"] != image
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
    receipt = LaunchReceipt(version=2, launch_id=launch_id, hostname=hostname,
        container_id=container_id, manifest_digest=canonical_digest(manifest.model_dump(mode="json")),
        image=manifest.image, actual_image_id=record["Image"], platform=manifest.platform)
    publish(receipt)
    runner(["docker", "start", container_id])
    return container_id
