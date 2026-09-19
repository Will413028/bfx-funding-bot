"""Build once, explicitly migrate, deploy exact application images halted.

prepare uses clean tracked source. Host commands require root; never build/pull,
recreate PG/Redis, consume permits, authorize/promote/resume, or overwrite receipts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from bfx_funding_bot.core.release_identity import (
    NONSECRET_ENV_KEYS,
    ReleaseManifest,
    canonical_digest,
    runtime_environment,
)
from scripts.release_package import (
    PackagingBlocked,
    create_launch,
    inspect_image,
    prepare_backend,
    prepare_frontend,
    run,
)


def validate_runtime_envs(manifest: ReleaseManifest, *, bot: dict[str, str], api: dict[str, str],
                          frontend: dict[str, str], public: dict[str, str]) -> None:
    try:
        if runtime_environment({**manifest.environment, **bot}) != manifest.environment:
            raise ValueError
        realm = manifest.environment.get("BFX_DEPLOYMENT_ENV")
        if not realm or api.get("BFX_DEPLOYMENT_ENV") != realm:
            raise ValueError
        operator = manifest.environment.get("BFX_OPERATOR_USER_ID")
        if not operator or any(env.get("BFX_OPERATOR_USER_ID") != operator or env.get("BFX_OPERATOR_ROLE") != "admin"
                               for env in (manifest.environment, api, frontend)):
            raise ValueError
        if any(frontend.get(key) != value for key, value in public.items()):
            raise ValueError
        for env, principal in ((bot, "bfx_bot"), (api, "bfx_webapi"), (frontend, "bfx_webauth")):
            if urlsplit(env["DATABASE_URL"]).username != principal:
                raise ValueError
        if any(key in bot for key in ("BFX_API_KEY", "BFX_API_SECRET")):
            raise ValueError
        if not bot.get("BFX_ADMIN_TOKEN") or bot["BFX_ADMIN_TOKEN"] != api.get("BFX_ADMIN_TOKEN"):
            raise ValueError
        if any(env.get(key) for env in (bot, api, frontend) for key in
               ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "LD_PRELOAD", "LD_LIBRARY_PATH", "NODE_OPTIONS")):
            raise ValueError
    except Exception:
        raise PackagingBlocked("runtime_environment_or_operator_mismatch") from None


def protected(path: Path, *, secret: bool = False) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise PackagingBlocked("protected_file_required")
    for entry in (path, *path.parents):
        metadata = entry.stat()
        if entry.is_symlink() or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise PackagingBlocked("host_root_control_required")
    if secret and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise PackagingBlocked("protected_env_mode_0600_required")


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


def write_new(path: Path, value: object) -> None:
    with path.open("x") as handle:
        json.dump(value, handle, sort_keys=True, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o444)


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    config = read_env(args.config)
    if not config or set(config) - NONSECRET_ENV_KEYS:
        raise PackagingBlocked("prepare_config_must_be_nonsecret")
    if config.get("BFX_PHASE") != "live" or config.get("BFX_EXECUTOR") != "bitfinex_live":
        raise PackagingBlocked("normal_live_profile_required")
    if args.output.exists():
        raise PackagingBlocked("new_output_directory_required")
    args.output.mkdir(mode=0o755)
    manifest, backend = prepare_backend(release_id=args.release_id, platform=args.platform, config_file=args.config)
    frontend = prepare_frontend(revision=manifest.source_revision, platform=args.platform,
                                public=json.loads(args.frontend_public.read_bytes()))
    archive = args.output / "images.tar"
    run(["docker", "save", "--output", str(archive), manifest.docker_image_id, frontend["docker_image_id"]])
    with archive.open("rb") as handle:
        archive_digest = hashlib.file_digest(handle, "sha256").hexdigest()
    archive.chmod(0o444)
    bundle = {"version": 1, "manifest": manifest.model_dump(mode="json"),
        "backend_preparation": backend, "frontend": frontend, "images_sha256": archive_digest,
        "migration": {"status": "not_applied", "target_head": manifest.schema_head,
                      "policy_conversion": "explicit_dry_run_then_digest_apply_required", "resumed": False}}
    write_new(args.output / "bundle.json", bundle)
    write_new(args.output / "manifest.json", manifest.model_dump(mode="json"))
    return {"status": "prepared_not_deployed", "bundle_digest": canonical_digest(bundle),
            "docker_image_id": manifest.docker_image_id, "frontend_image_id": frontend["docker_image_id"]}


def read_bundle(path: Path) -> tuple[dict[str, Any], ReleaseManifest]:
    protected(path)
    bundle = json.loads(path.read_bytes())
    manifest = ReleaseManifest.model_validate(bundle["manifest"])
    backend, frontend = bundle["backend_preparation"], bundle["frontend"]
    if (bundle["version"] != 1
        or backend["manifest_digest"] != canonical_digest(manifest.model_dump(mode="json"))
        or backend["source_revision"] != manifest.source_revision
        or backend["docker_image_id"] != manifest.docker_image_id
        or frontend["source_revision"] != manifest.source_revision
        or frontend["platform"] != manifest.platform):
        raise PackagingBlocked("manifest_preparation_mismatch")
    inspect_image(manifest.docker_image_id, manifest.platform, run)
    inspect_image(bundle["frontend"]["docker_image_id"], manifest.platform, run)
    return bundle, manifest


def one_shot(manifest: ReleaseManifest, *, env: Path, network: str, command: list[str],
             extra_env: dict[str, str] | None = None, mounts: list[str] | None = None) -> bytes:
    protected(env, secret=True)
    args = ["docker", "run", "--rm", "--pull=never", "--read-only", "--user", "1000:1000",
        "--workdir", "/app", "--network", network, "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--env-file", str(env)]
    for key, value in (extra_env or {}).items():
        args += ["--env", f"{key}={value}"]
    for mount in mounts or []:
        args += ["--mount", mount]
    return run([*args, manifest.docker_image_id, *command])


def migrate(args: argparse.Namespace, manifest: ReleaseManifest) -> dict[str, Any]:
    def schema() -> dict[str, Any]:
        return json.loads(one_shot(manifest, env=args.env, network=args.network,
            command=["/app/.venv/bin/python", "-m", "scripts.release_database", "schema"]))
    before = schema()
    plan = {"version": 1, "image_id": manifest.docker_image_id, "before": before,
            "command": ["uv", "run", "alembic", "upgrade", "head"], "resumed": False}
    digest = canonical_digest(plan)
    if args.apply_digest:
        if args.apply_digest != digest:
            raise PackagingBlocked("migration_plan_changed")
        one_shot(manifest, env=args.env, network=args.network,
            command=plan["command"], extra_env={"UV_NO_SYNC": "1", "UV_CACHE_DIR": "/tmp/uv"})
        after = schema()
        if after["schema_heads"] != [manifest.schema_head]:
            raise PackagingBlocked("migration_head_mismatch")
        return {**plan, "status": "applied", "digest": digest, "after": after}
    return {**plan, "status": "dry_run", "digest": digest}


def start_service(*, image: str, platform: str, name: str, user: str, env: Path,
                  network: str, command: list[str], port: str | None = None) -> str:
    inspect_image(image, platform, run)
    args = ["docker", "create", "--pull=never", "--read-only", "--user", user, "--entrypoint", "",
        "--workdir", "/app", "--name", name, "--network", network,
        "--cap-drop=ALL", "--security-opt=no-new-privileges", "--env-file", str(env)]
    if port:
        args += ["--publish", port]
    cid = run([*args, image, *command]).decode().strip()
    inspected = json.loads(run(["docker", "inspect", cid]))[0]
    config, host = inspected["Config"], inspected["HostConfig"]
    observed_env = dict(item.split("=", 1) for item in config["Env"])
    if (inspected["Id"] != cid or inspected["Image"] != image or inspected["Platform"] != "linux"
        or inspected["State"]["Running"] or config["Cmd"] != command
        or config.get("Entrypoint") or config["User"] != user or config["WorkingDir"] != "/app"
        or not host["ReadonlyRootfs"] or host["Privileged"] or host.get("CapAdd")
        or "ALL" not in (host.get("CapDrop") or [])
        or "no-new-privileges" not in (host.get("SecurityOpt") or [])
        or host["NetworkMode"] != network or inspected["Mounts"]
        or any(observed_env.get(key) != value for key, value in read_env(env).items())):
        raise PackagingBlocked("service_launch_mismatch")
    run(["docker", "start", cid])
    return cid


def deploy(args: argparse.Namespace, bundle: dict[str, Any], manifest: ReleaseManifest) -> dict[str, Any]:
    for path in (args.bot_env, args.webapi_env, args.frontend_env):
        protected(path, secret=True)
    bot, api, frontend = (read_env(path) for path in (args.bot_env, args.webapi_env, args.frontend_env))
    validate_runtime_envs(manifest, bot=bot, api=api, frontend=frontend,
                         public=bundle["frontend"]["public_environment"])
    protected(args.halt2)
    infrastructure = json.loads(run(["docker", "inspect", "bfx-postgres", "bfx-redis"]))
    if not all(row["State"]["Running"] for row in infrastructure):
        raise PackagingBlocked("existing_postgres_and_redis_required")
    run(["docker", "network", "inspect", args.network])
    old = json.loads(run(["docker", "inspect", "bfx-bot", "bfx-webapi", "bfx-frontend"]))
    if any(row["State"]["Running"] for row in old):
        raise PackagingBlocked("quiescent_application_containers_required")
    proof = json.loads(one_shot(manifest, env=args.bot_env, network=args.network,
        extra_env=manifest.environment,
        command=["/app/.venv/bin/python", "-m", "scripts.release_database", "startup"]))
    launch_dir = args.bundle.parent / ("launch-" + uuid4().hex)
    launch_dir.mkdir(mode=0o755)
    write_new(launch_dir / "manifest.json", manifest.model_dump(mode="json"))
    shutil.copyfile(args.halt2, launch_dir / "halt2.json")
    (launch_dir / "halt2.json").chmod(0o444)
    evidence = json.loads(args.halt2.read_bytes())
    evidence_mounts = {}
    for key in ("backup_evidence_path", "isolated_restore_evidence_path"):
        destination = evidence[key]
        if not re.fullmatch(r"/run/bfx-dr/[a-zA-Z0-9_-]+\.json", destination) or destination in evidence_mounts:
            raise PackagingBlocked("invalid_evidence_destination")
        source = args.dr_directory / Path(destination).name
        protected(source, secret=True)
        target = launch_dir / Path(destination).name
        shutil.copyfile(source, target)
        target.chmod(0o600)
        os.chown(target, 1000, 1000)
        evidence_mounts[destination] = target
    # Execute the existing artifact/DR consumer in the approved image at its
    # actual runtime UID and paths, before touching stopped application names.
    one_shot(manifest, env=args.bot_env, network=args.network,
        mounts=[f"type=bind,src={launch_dir},dst=/run/bfx-release,readonly",
                *[f"type=bind,src={src},dst={dst},readonly" for dst, src in evidence_mounts.items()]],
        command=["/app/.venv/bin/python", "-c",
            "from pathlib import Path; from scripts.halt2_cutover import _load_evidence; "
            "from scripts.run_canary_preflight import _require_halt2_artifacts; "
            "e=_load_evidence(Path('/run/bfx-release/halt2.json')); "
            "_require_halt2_artifacts(e,Path(__import__('sys').argv[1]))",
            manifest.environment["BFX_SAFETY_CONFIG"]])
    config = launch_dir / "config.env"
    config.write_text("".join(f"{k}={v}\n" for k, v in sorted(manifest.environment.items())))
    config.chmod(0o444)
    suffix = launch_dir.name
    for row in old:
        run(["docker", "rename", row["Id"], row["Name"].lstrip("/") + "-before-" + suffix])
    bot_id = create_launch(manifest, release_dir=launch_dir,
        env_files=[args.bot_env, config], network=args.network,
        evidence_mounts=evidence_mounts,
        publish=lambda receipt: write_new(launch_dir / "launch.json", receipt.model_dump(mode="json")))
    run(["docker", "rename", bot_id, "bfx-bot"])
    api_id = start_service(image=manifest.docker_image_id, platform=manifest.platform,
        name="bfx-webapi", user="1000:1000", env=args.webapi_env, network=args.network,
        command=["/app/.venv/bin/python", "-m", "uvicorn", "bfx_funding_bot.main:app", "--host", "0.0.0.0", "--port", "8000"])
    frontend_id = start_service(image=bundle["frontend"]["docker_image_id"], platform=manifest.platform,
        name="bfx-frontend", user="nextjs", env=args.frontend_env, network=args.network,
        command=["node", "server.js"], port="127.0.0.1:3001:3000")
    after = json.loads(run(["docker", "inspect", "bfx-postgres", "bfx-redis"]))
    if [(r["Id"], r["Mounts"]) for r in infrastructure] != [(r["Id"], r["Mounts"]) for r in after]:
        raise PackagingBlocked("infrastructure_changed")
    halt_after = json.loads(one_shot(manifest, env=args.bot_env, network=args.network,
        extra_env=manifest.environment,
        command=["/app/.venv/bin/python", "-m", "scripts.release_database", "startup"]))
    if halt_after != proof:
        raise PackagingBlocked("startup_state_changed")
    receipt = {"status": "technical_start_only", "halt_before": proof, "halt_after": halt_after, "resumed": False,
        "containers": {"bot": bot_id, "webapi": api_id, "frontend": frontend_id},
        "bundle_digest": canonical_digest(bundle), "human_activation_required": True}
    write_new(launch_dir / "deployment.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--release-id", required=True)
    prep.add_argument("--platform", choices=("linux/arm64", "linux/amd64"), default="linux/arm64")
    prep.add_argument("--config", type=Path, required=True)
    prep.add_argument("--frontend-public", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    for name in ("migrate", "bootstrap", "policy", "deploy"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--bundle", type=Path, required=True)
        cmd.add_argument("--network", required=True)
        if name == "deploy":
            for value in ("bot-env", "webapi-env", "frontend-env", "halt2", "dr-directory"):
                cmd.add_argument("--" + value, type=Path, required=True)
        else:
            cmd.add_argument("--env", type=Path, required=True)
            cmd.add_argument("--receipt", type=Path, required=True)
            if name in {"migrate", "policy"}:
                cmd.add_argument("--apply-digest")
            if name == "policy":
                cmd.add_argument("--legacy-source", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            result = prepare(args)
        else:
            if os.geteuid() != 0:
                raise PackagingBlocked("trusted_host_root_required")
            if args.action != "deploy" and (not args.receipt.is_absolute()
                or args.receipt.exists() or args.receipt.is_symlink()):
                raise PackagingBlocked("new_receipt_path_required")
            bundle, manifest = read_bundle(args.bundle)
            if args.action == "deploy":
                result = deploy(args, bundle, manifest)
            elif args.action == "migrate":
                result = migrate(args, manifest)
                write_new(args.receipt, result)
            else:
                account = manifest.environment["BFX_EXCHANGE_ACCOUNT_ID"]
                environment = manifest.environment["BFX_DEPLOYMENT_ENV"]
                command = ["/app/.venv/bin/python", "-m", "scripts.bootstrap_capital",
                           "--account-id", account, "--environment", environment]
                mounts = []
                if args.action == "policy":
                    protected(args.legacy_source)
                    mounts = [f"type=bind,src={args.legacy_source},dst=/run/legacy.json,readonly"]
                    command = ["/app/.venv/bin/python", "-m", "scripts.convert_capital_policy",
                        "--exchange-account-id", account, "--environment", environment,
                        "--legacy-source", "/run/legacy.json", "--max-snapshot-age-ms", "300000"]
                    if args.apply_digest:
                        command += ["--apply-digest", args.apply_digest]
                result = json.loads(one_shot(manifest, env=args.env, network=args.network,
                                             command=command, mounts=mounts))
                write_new(args.receipt, result)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        reason = str(exc) if isinstance(exc, PackagingBlocked) else "release_operation_failed"
        print(json.dumps({"status": "blocked", "reason": reason, "resumed": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
