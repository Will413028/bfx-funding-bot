"""Host producer behavior against a controlled Docker command boundary."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from bfx_funding_bot.core.release_identity import ReleaseManifest

IMAGE = "sha256:" + "a" * 64


def manifest() -> ReleaseManifest:
    return ReleaseManifest(version=1, release_id="fixture", source_revision="b" * 40,
        platform="linux/arm64", docker_image_id=IMAGE, oci_manifest_digest=None,
        inventory={"configs/cells.yaml": "c" * 64}, python_inventory={},
        environment={"BFX_PHASE": "live"}, schema_head="b4e6f8a0c203",
        projector_version="execution-state-v1")


class Docker:
    def __init__(self, mutation: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.mutation = mutation
        self.container: dict = {}

    def __call__(self, args: list[str], *, data: bytes | None = None) -> bytes:
        self.calls.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": IMAGE, "Os": "linux", "Architecture": "arm64"}]).encode()
        if args[:2] == ["docker", "create"]:
            self.container = {
                "Id": "d" * 64, "Image": IMAGE, "Platform": "linux",
                "Config": {"Hostname": args[args.index("--hostname") + 1],
                    "User": "1000:1000", "WorkingDir": "/app",
                    "Entrypoint": None, "Cmd": ["/app/.venv/bin/python", "-m",
                        "bfx_funding_bot.modules.marketfeed.daemon"],
                    "Env": ["BFX_PHASE=live", "PYTHONDONTWRITEBYTECODE=1"]},
                "HostConfig": {"ReadonlyRootfs": True, "Privileged": False,
                    "NetworkMode": "fixture", "CapAdd": None, "Binds": None,
                    "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"]},
                "Mounts": [{"Destination": mount.split("dst=")[1].split(",")[0],
                    "RW": False, "Source": mount.split("src=")[1].split(",")[0]}
                    for i, mount in enumerate(args) if i and args[i-1] == "--mount"],
                "State": {"Running": False},
            }
            return ("d" * 64).encode()
        if args[:2] == ["docker", "inspect"]:
            inspected = deepcopy(self.container)
            if self.mutation == "image":
                inspected["Image"] = "sha256:" + "e" * 64
            elif self.mutation == "config":
                inspected["Config"]["Env"].append("BFX_KILL_SWITCH=true")
            elif self.mutation == "writable":
                inspected["Mounts"].append({"Destination": "/app", "RW": True})
            elif self.mutation == "command":
                inspected["Config"]["Cmd"] = ["sh", "-c", "alembic upgrade head"]
            elif self.mutation == "capability":
                inspected["HostConfig"]["CapDrop"] = []
            elif self.mutation == "security":
                inspected["HostConfig"]["SecurityOpt"] = []
            return json.dumps([inspected]).encode()
        if args[:2] == ["docker", "start"]:
            return b"started"
        raise AssertionError(f"unanticipated command: {args[:3]}")


def test_deploy_starts_only_measured_image_after_publishing_receipt(tmp_path: Path) -> None:
    """A moving tag, implicit build or start-before-receipt must fail this test."""
    from scripts.release_package import create_launch

    docker = Docker()
    receipts = []
    def runner(args, *, data=None):
        if args[:2] == ["docker", "start"]:
            assert len(receipts) == 1, "start preceded publication"
        return docker(args, data=data)
    result = create_launch(manifest(), release_dir=tmp_path, env_files=[], network="fixture",
        runner=runner, publish=receipts.append)
    assert receipts[0].container_id == "d" * 64
    assert receipts[0].docker_image_id == IMAGE
    assert result == "d" * 64
    assert not any("build" in call or "pull" in call for call in docker.calls)
    assert docker.calls[-1] == ["docker", "start", "d" * 64]
    assert "--read-only" in docker.calls[1]
    assert IMAGE in docker.calls[1]


@pytest.mark.parametrize("mutation", ["image", "config", "writable", "command", "capability", "security"])
def test_deploy_rejects_inspected_drift_before_start(tmp_path: Path, mutation: str) -> None:
    from scripts.release_package import PackagingBlocked, create_launch

    docker = Docker(mutation)
    receipts = []
    with pytest.raises(PackagingBlocked):
        create_launch(manifest(), release_dir=tmp_path, env_files=[], network="fixture",
            runner=docker, publish=receipts.append)
    assert not receipts
    assert not any(call[:2] == ["docker", "start"] for call in docker.calls)


def test_platform_mismatch_never_creates_container(tmp_path: Path) -> None:
    from scripts.release_package import PackagingBlocked, create_launch

    docker = Docker()
    release = manifest().model_copy(update={"platform": "linux/amd64"})
    with pytest.raises(PackagingBlocked):
        create_launch(release, release_dir=tmp_path, env_files=[], network="fixture",
            runner=docker, publish=lambda _: None)
    assert len(docker.calls) == 1


def test_launch_mounts_dr_receipts_readonly_without_shadowing_inventory(tmp_path):
    from scripts.release_package import PackagingBlocked, create_launch
    docker = Docker()
    create_launch(manifest(), release_dir=tmp_path, env_files=[], network="fixture", runner=docker,
        evidence_mounts={"/run/bfx-dr/backup.json": tmp_path / "backup.json"}, publish=lambda _: None)
    assert {"Destination": "/run/bfx-dr/backup.json", "RW": False,
            "Source": str(tmp_path / "backup.json")} in docker.container["Mounts"]
    assert "--network-alias" in docker.calls[1]
    with pytest.raises(PackagingBlocked, match="evidence_mount"):
        create_launch(manifest(), release_dir=tmp_path, env_files=[], network="fixture", runner=docker,
            evidence_mounts={"/app/configs/safety.yaml": tmp_path / "backup.json"}, publish=lambda _: None)


def test_prepare_builds_clean_tracked_archive_and_measures_actual_image(tmp_path: Path) -> None:
    from scripts.release_package import prepare_backend

    calls = []

    def runner(args, *, data=None):
        calls.append((args, data))
        if args[:3] == ["git", "status", "--porcelain"]:
            return b""
        if args[:2] == ["git", "rev-parse"]:
            return ("b" * 40).encode()
        if args[:2] == ["git", "archive"]:
            return b"tracked-source-tar"
        if args[:2] == ["docker", "build"]:
            assert data == b"tracked-source-tar"
            return IMAGE.encode()
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": IMAGE, "Os": "linux", "Architecture": "arm64",
                                "RepoDigests": []}]).encode()
        if args[:2] == ["docker", "run"]:
            assert IMAGE in args
            return json.dumps({"inventory": {"src/app.py": "f" * 64},
                "python_inventory": {"bin/python3.13": "e" * 64},
                "environment": {"BFX_PHASE": "live"}, "schema_head": "b4e6f8a0c203",
                "projector_version": "execution-state-v1", "measurement_seconds": 2.5}).encode()
        raise AssertionError(args)

    release, receipt = prepare_backend(release_id="fixture", platform="linux/arm64",
        config_file=tmp_path / "nonsecret.env", runner=runner)
    assert release.docker_image_id == IMAGE
    assert release.oci_manifest_digest is None
    assert release.python_inventory == {"bin/python3.13": "e" * 64}
    assert receipt["measurement_seconds"] == 2.5
    assert sum(args[:2] == ["docker", "build"] for args, _ in calls) == 1


def test_prepare_refuses_dirty_source_before_build(tmp_path: Path) -> None:
    from scripts.release_package import PackagingBlocked, prepare_backend

    calls = []
    def runner(args, *, data=None):
        calls.append(args)
        return b" M backend_py/Dockerfile"

    with pytest.raises(PackagingBlocked, match="source_not_clean"):
        prepare_backend(release_id="fixture", platform="linux/arm64",
            config_file=tmp_path / "nonsecret.env", runner=runner)
    assert len(calls) == 1


def test_prepare_frontend_binds_public_build_configuration(tmp_path):
    from scripts.release_package import prepare_frontend
    calls = []
    public = {"NEXT_PUBLIC_APP_URL": "https://example.test", "NEXT_PUBLIC_APP_NAME": "fixture",
              "NEXT_PUBLIC_BETTER_AUTH_URL": "https://example.test"}
    def runner(args, *, data=None):
        calls.append(args)
        if args[:2] == ["git", "archive"]:
            return b"frontend-archive"
        if args[:2] == ["docker", "build"]:
            assert data == b"frontend-archive"
            return IMAGE.encode()
        return json.dumps([{"Id": IMAGE, "Os": "linux", "Architecture": "arm64"}]).encode()
    receipt = prepare_frontend(revision="b" * 40, platform="linux/arm64", public=public, runner=runner)
    assert receipt["public_environment"] == public
    assert receipt["docker_image_id"] == IMAGE
    build = calls[1]
    assert "NEXT_PUBLIC_APP_URL=https://example.test" in build
    assert "NEXT_PUBLIC_BETTER_AUTH_URL=https://example.test" in build


def test_frontend_preparation_rejects_secret_or_incomplete_build_configuration():
    from scripts.release_package import PackagingBlocked, prepare_frontend
    with pytest.raises(PackagingBlocked, match="public_configuration"):
        prepare_frontend(revision="b" * 40, platform="linux/arm64", public={"SECRET": "do-not-print"})


@pytest.mark.parametrize("bad", ["operator", "role", "public_url", "principal", "legacy", "admin_token", "injection"])
def test_runtime_env_validation_blocks_drift_without_exposing_secrets(bad):
    from scripts.immutable_release import validate_runtime_envs
    from scripts.release_package import PackagingBlocked
    release = manifest().model_copy(update={"environment": {"BFX_PHASE": "live", "BFX_DEPLOYMENT_ENV": "prod", "BFX_OPERATOR_USER_ID": "operator", "BFX_OPERATOR_ROLE": "admin"}})
    bot = {"DATABASE_URL": "postgresql://bfx_bot:secret@db/bfx", "BFX_VAULT_KEK": "secret", "BFX_ADMIN_TOKEN": "secret"}
    api = {"DATABASE_URL": "postgresql://bfx_webapi:secret@db/bfx", "BFX_DEPLOYMENT_ENV": "prod", "BFX_OPERATOR_USER_ID": "operator", "BFX_OPERATOR_ROLE": "admin", "BFX_ADMIN_TOKEN": "secret"}
    frontend = {"DATABASE_URL": "postgresql://bfx_webauth:secret@db/bfx", "BFX_OPERATOR_USER_ID": "operator", "BFX_OPERATOR_ROLE": "admin", "NEXT_PUBLIC_APP_URL": "https://example.test"}
    public = {"NEXT_PUBLIC_APP_URL": "https://example.test"}
    validate_runtime_envs(release, bot=bot, api=api, frontend=frontend, public=public)
    if bad == "operator":
        api["BFX_OPERATOR_USER_ID"] = "other"
    elif bad == "role":
        frontend["BFX_OPERATOR_ROLE"] = "user"
    elif bad == "public_url":
        frontend["NEXT_PUBLIC_APP_URL"] = "https://wrong.test"
    elif bad == "principal":
        bot["DATABASE_URL"] = "postgresql://bfx:secret@db/bfx"
    elif bad == "admin_token":
        api["BFX_ADMIN_TOKEN"] = "different-secret"
    elif bad == "injection":
        frontend["NODE_OPTIONS"] = "--require /tmp/inject-secret.js"
    else:
        bot["BFX_ALLOCATION_CAP_USDT"] = "secret"
    with pytest.raises(PackagingBlocked) as caught:
        validate_runtime_envs(release, bot=bot, api=api, frontend=frontend, public=public)
    assert "secret" not in str(caught.value)


def test_legacy_deploy_invocation_is_rejected_before_any_commands(tmp_path):
    import subprocess
    root = Path(__file__).resolve().parents[3]
    commands = tmp_path / "commands"
    commands.mkdir()
    for name in ("git", "docker", "uv", "ssh"):
        executable = commands / name
        executable.write_text("#!/bin/sh\necho external-command-blocked >&2\nexit 91\n")
        executable.chmod(0o755)
    result = subprocess.run(["bash", str(root / "scripts/deploy-vm.sh"), "canary"],
        cwd=tmp_path, capture_output=True, text=True, env={"PATH": f"{commands}:/usr/bin:/bin"})
    assert result.returncode == 2
    assert "immutable" in result.stderr


def test_migration_dry_run_never_applies_and_stale_digest_blocks(monkeypatch):
    from types import SimpleNamespace

    from scripts import immutable_release as cli
    from scripts.release_package import PackagingBlocked
    calls = []
    def execute(*args, **kwargs):
        calls.append(kwargs["command"])
        return b'{"schema_heads":["previous"],"target_head":"b4e6f8a0c203","resumed":false}'
    monkeypatch.setattr(cli, "one_shot", execute)
    args = SimpleNamespace(env=Path("/fixture"), network="fixture", apply_digest=None)
    dry = cli.migrate(args, manifest())
    assert dry["status"] == "dry_run"
    assert len(calls) == 1
    args.apply_digest = "not-the-reviewed-digest"
    with pytest.raises(PackagingBlocked, match="migration_plan_changed"):
        cli.migrate(args, manifest())
    assert all("alembic" not in call for call in calls)
