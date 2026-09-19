"""Host producer behavior against a controlled Docker command boundary."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from bfx_funding_bot.core.release_identity import PackagedImageIdentity, ReleaseManifest

IMAGE = "sha256:" + "a" * 64
IDENTITY = PackagedImageIdentity(config_digest=IMAGE, manifest_digest="sha256:" + "f" * 64,
                                platform="linux/arm64")


def manifest() -> ReleaseManifest:
    return ReleaseManifest(version=2, release_id="fixture", source_revision="b" * 40,
        image=IDENTITY,
        inventory={"configs/cells.yaml": "c" * 64}, python_inventory={},
        environment={"BFX_PHASE": "live"}, schema_head="b4e6f8a0c203",
        projector_version="execution-state-v1")


class Docker:
    def __init__(self, mutation: str | None = None, store: str = "classic") -> None:
        self.calls: list[list[str]] = []
        self.mutation = mutation
        self.container: dict = {}
        self.image = IMAGE if store == "classic" else IDENTITY.manifest_digest

    def __call__(self, args: list[str], *, data: bytes | None = None) -> bytes:
        self.calls.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            from scripts.image_artifact import ImageNotFound
            if args[-1] != self.image:
                raise ImageNotFound("image_not_found")
            record = {"Id": self.image, "Os": "linux", "Architecture": "arm64"}
            if self.image != IMAGE:
                record["Descriptor"] = {"digest": self.image,
                    "mediaType": "application/vnd.oci.image.manifest.v1+json"}
            return json.dumps([record]).encode()
        if args[:2] == ["docker", "create"]:
            self.container = {
                "Id": "d" * 64, "Image": self.image, "Platform": "linux",
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


class OneShotDocker:
    """Small Docker boundary with inspected settings, attached output and exit."""
    def __init__(self, image=IMAGE, mutation=None, output=b"verified", exit_code=0):
        self.image, self.mutation, self.output, self.exit_code = image, mutation, output, exit_code
        self.calls = []
        self.cid = "d"*64
        self.record = {}
        self.started = False

    def __call__(self, args, *, data=None):
        from scripts.image_artifact import ImageNotFound
        from scripts.release_package import PackagingBlocked
        self.calls.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            if args[-1] != self.image:
                raise ImageNotFound("image_not_found")
            record = {"Id": self.image, "Os": "linux", "Architecture": "arm64"}
            if self.image == IDENTITY.manifest_digest:
                record["Descriptor"] = {"digest": self.image, "mediaType": "application/vnd.oci.image.manifest.v1+json"}
            return json.dumps([record]).encode()
        if args[:2] == ["docker", "run"]:  # old code executes without inspection
            self.started = True
            return self.output
        if args[:2] == ["docker", "create"]:
            env = {"PYTHONDONTWRITEBYTECODE": "1"}
            mounts, tmpfs = [], {}
            for index, arg in enumerate(args):
                if arg == "--env-file":
                    env.update(line.split("=", 1) for line in Path(args[index+1]).read_text().splitlines() if line)
                if arg == "--env":
                    key, value = args[index+1].split("=", 1)
                    env[key] = value
                if arg == "--tmpfs":
                    key, value = args[index+1].split(":", 1)
                    tmpfs[key] = value
                if arg == "--mount":
                    fields = dict(item.split("=", 1) for item in args[index+1].split(",") if "=" in item)
                    mounts.append({"Type": fields["type"], "Source": fields["src"],
                                   "Destination": fields["dst"], "RW": "readonly" not in args[index+1]})
            self.record = {"Id": self.cid, "Image": self.image, "Platform": "linux",
                "State": {"Running": False, "Status": "created", "ExitCode": 0},
                "Config": {"Cmd": args[args.index(self.image)+1:], "Entrypoint": None,
                    "User": args[args.index("--user")+1] if "--user" in args else "appuser",
                    "WorkingDir": "/app", "Env": [f"{k}={v}" for k, v in env.items()]},
                "HostConfig": {"ReadonlyRootfs": "--read-only" in args, "Privileged": False,
                    "NetworkMode": args[args.index("--network")+1], "CapAdd": None,
                    "CapDrop": ["ALL"] if "--cap-drop=ALL" in args else [],
                    "SecurityOpt": ["no-new-privileges"] if "--security-opt=no-new-privileges" in args else [],
                    "Tmpfs": tmpfs}, "Mounts": mounts}
            return self.cid.encode()
        if args[:2] == ["docker", "inspect"]:
            assert args[-1] == self.cid
            record = deepcopy(self.record)
            if not self.started:
                if self.mutation in {"Image", "Id"}:
                    record[self.mutation] = "e"*64
                elif self.mutation == "Command":
                    record["Config"]["Cmd"] = ["wrong-command"]
                elif self.mutation == "ReadonlyRootfs":
                    record["HostConfig"]["ReadonlyRootfs"] = False
                elif self.mutation == "NetworkMode":
                    record["HostConfig"]["NetworkMode"] = "wrong-network"
                elif self.mutation == "Env":
                    record["Config"]["Env"] = ["PYTHONDONTWRITEBYTECODE=0"]
                elif self.mutation == "Mounts":
                    record["Mounts"].append({"Type": "bind", "Source": "/tmp", "Destination": "/app", "RW": True})
                elif self.mutation == "Tmpfs":
                    record["HostConfig"]["Tmpfs"] = {"/app": "rw"}
            return json.dumps([record]).encode()
        if args[:2] == ["docker", "start"]:
            assert args == ["docker", "start", "--attach", self.cid]
            self.started = True
            self.record["State"] = {"Running": False, "Status": "exited", "ExitCode": self.exit_code}
            if self.mutation == "start_error":
                raise PackagingBlocked("command_failed:docker")
            return self.output
        if args[:2] == ["docker", "rm"]:
            assert args[-1] == self.cid
            return b""
        raise AssertionError(args[:3])


@pytest.mark.parametrize("store", ["classic", "containerd"])
def test_deploy_starts_only_measured_image_after_publishing_receipt(tmp_path: Path, store) -> None:
    """A moving tag, implicit build or start-before-receipt must fail this test."""
    from scripts.release_package import create_launch

    docker = Docker(store=store)
    receipts = []
    def runner(args, *, data=None):
        if args[:2] == ["docker", "start"]:
            assert len(receipts) == 1, "start preceded publication"
        return docker(args, data=data)
    result = create_launch(manifest(), release_dir=tmp_path, env_files=[], network="fixture",
        runner=runner, publish=receipts.append)
    assert receipts[0].container_id == "d" * 64
    assert receipts[0].actual_image_id == docker.image
    assert receipts[0].image == IDENTITY
    assert result == "d" * 64
    assert not any("build" in call or "pull" in call for call in docker.calls)
    assert docker.calls[-1] == ["docker", "start", "d" * 64]
    create = next(call for call in docker.calls if call[:2] == ["docker", "create"])
    assert "--read-only" in create
    assert docker.image in create


@pytest.mark.parametrize("store", ["classic", "containerd"])
@pytest.mark.parametrize("mutation", ["image", "config", "writable", "command", "capability", "security"])
def test_deploy_rejects_inspected_drift_before_start(tmp_path: Path, mutation: str, store) -> None:
    from scripts.release_package import PackagingBlocked, create_launch

    docker = Docker(mutation, store)
    receipts = []
    with pytest.raises(PackagingBlocked):
        create_launch(manifest(), release_dir=tmp_path, env_files=[], network="fixture",
            runner=docker, publish=receipts.append)
    assert not receipts
    assert not any(call[:2] == ["docker", "start"] for call in docker.calls)


def test_platform_mismatch_never_creates_container(tmp_path: Path) -> None:
    from scripts.release_package import PackagingBlocked, create_launch

    docker = Docker()
    release = manifest().model_copy(update={"image": IDENTITY.model_copy(update={"platform": "linux/amd64"})})
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


@pytest.mark.parametrize("mutation", [None, "Image"])
def test_prepare_builds_clean_tracked_archive_and_measures_actual_image(tmp_path: Path, mutation) -> None:
    from scripts.release_package import prepare_backend
    from tests.scripts.test_image_artifact import archive_fixture
    archive = tmp_path / "backend.tar"
    expected = archive_fixture(tmp_path / "expected.tar")
    image = expected["config_digest"]
    env = tmp_path / "nonsecret.env"
    env.write_text("BFX_PHASE=live\n")
    docker = OneShotDocker(image=image, mutation=mutation, output=json.dumps({
        "inventory": {"src/app.py": "f" * 64}, "python_inventory": {"bin/python3.13": "e" * 64},
        "environment": {"BFX_PHASE": "live"}, "schema_head": "b4e6f8a0c203",
        "projector_version": "execution-state-v1", "measurement_seconds": 2.5}).encode())

    calls = []

    def runner(args, *, data=None):
        calls.append((args, data))
        if "status" in args:
            return b""
        if args[0] == "git" and "rev-parse" in args:
            return ("b" * 40).encode()
        if args[0] == "git" and "show" in args:
            return b"1700000000"
        if args[0] == "git" and "archive" in args:
            return b"tracked-source-tar"
        if args[:2] == ["docker", "build"]:
            assert data == b"tracked-source-tar"
            return image.encode()
        if args[:2] == ["docker", "save"]:
            assert args[-1] == image
            assert args[-2] == str(archive)
            archive_fixture(archive)
            return b""
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": image, "Os": "linux", "Architecture": "arm64",
                                "RepoDigests": []}]).encode()
        if args[0] == "docker":
            return docker(args, data=data)
        raise AssertionError(args)

    if mutation:
        from scripts.release_package import PackagingBlocked
        with pytest.raises(PackagingBlocked):
            prepare_backend(release_id="fixture", platform="linux/arm64",
                config_file=env, archive_path=archive, runner=runner)
        assert not docker.started
        assert docker.calls[-1] == ["docker", "rm", "--force", "--volumes", docker.cid]
        return
    release, receipt = prepare_backend(release_id="fixture", platform="linux/arm64",
        config_file=env, archive_path=archive, runner=runner)
    assert docker.calls.index(["docker", "inspect", docker.cid]) < docker.calls.index(
        ["docker", "start", "--attach", docker.cid])
    assert release.image.model_dump() == expected
    assert receipt["archive_filename"] == "backend.tar"
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
            config_file=tmp_path / "nonsecret.env", archive_path=tmp_path / "backend.tar", runner=runner)
    assert not any(call[:2] == ["docker", "build"] for call in calls)


def test_prepare_frontend_binds_public_build_configuration(tmp_path):
    from scripts.release_package import prepare_frontend
    from tests.scripts.test_image_artifact import archive_fixture
    archive = tmp_path / "frontend.tar"
    expected = archive_fixture(tmp_path / "expected.tar", component="frontend")
    image = expected["config_digest"]
    calls = []
    public = {"NEXT_PUBLIC_APP_URL": "https://example.test", "NEXT_PUBLIC_APP_NAME": "fixture",
              "NEXT_PUBLIC_BETTER_AUTH_URL": "https://example.test"}
    def runner(args, *, data=None):
        calls.append(args)
        if args[0] == "git" and "archive" in args:
            return b"frontend-archive"
        if args[0] == "git":
            return b"1700000000"
        if args[:2] == ["docker", "save"]:
            archive_fixture(archive, component="frontend")
            return b""
        if args[:2] == ["docker", "build"]:
            assert data == b"frontend-archive"
            return image.encode()
        return json.dumps([{"Id": image, "Os": "linux", "Architecture": "arm64"}]).encode()
    receipt = prepare_frontend(revision="b" * 40, platform="linux/arm64", public=public,
                               archive_path=archive, runner=runner)
    assert receipt["public_environment"] == public
    assert receipt["image"] == expected
    build = next(call for call in calls if call[:2] == ["docker", "build"])
    assert "NEXT_PUBLIC_APP_URL=https://example.test" in build
    assert "NEXT_PUBLIC_BETTER_AUTH_URL=https://example.test" in build


def test_frontend_preparation_rejects_secret_or_incomplete_build_configuration(tmp_path):
    from scripts.release_package import PackagingBlocked, prepare_frontend
    with pytest.raises(PackagingBlocked, match="public_configuration"):
        prepare_frontend(revision="b" * 40, platform="linux/arm64", public={"SECRET": "do-not-print"},
                         archive_path=tmp_path / "frontend.tar")


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


@pytest.mark.parametrize("corrupt", [None, "backend", "frontend"])
def test_prepare_publishes_only_after_two_independent_verified_exports(tmp_path, monkeypatch, corrupt):
    from types import SimpleNamespace

    from scripts import immutable_release as cli
    from scripts import release_package as producer
    from tests.scripts.test_image_artifact import archive_fixture
    expected = {component: archive_fixture(tmp_path / (component + "-expected.tar"), component=component)
                for component in ("backend", "frontend")}
    args = SimpleNamespace(output=tmp_path / "release", config=tmp_path / "config.env",
        frontend_public=tmp_path / "public.json", release_id="fixture", platform="linux/arm64")
    args.config.write_text("BFX_PHASE=live\nBFX_EXECUTOR=bitfinex_live\n")
    args.frontend_public.write_text(json.dumps({"NEXT_PUBLIC_APP_URL": "https://example.test",
        "NEXT_PUBLIC_APP_NAME": "fixture", "NEXT_PUBLIC_BETTER_AUTH_URL": "https://example.test"}))
    calls = []
    docker = OneShotDocker(image=expected["backend"]["config_digest"], output=json.dumps({
        "inventory": {}, "python_inventory": {},
        "environment": {"BFX_PHASE": "live", "BFX_EXECUTOR": "bitfinex_live"},
        "schema_head": "b4e6f8a0c203", "projector_version": "execution-state-v1",
        "measurement_seconds": 1}).encode())
    def runner(command, *, data=None):
        calls.append(command)
        if command[0] == "git":
            if "status" in command:
                return b""
            if "show" in command:
                return b"1700000000"
            if "archive" in command:
                return command[-1].encode()
            return ("b"*40).encode()
        if command[:2] == ["docker", "build"]:
            component = "backend" if data.endswith(b":backend_py") else "frontend"
            return expected[component]["config_digest"].encode()
        if command[:2] == ["docker", "save"]:
            assert len(command) == 5  # exactly one component, never a pair export
            component = next(name for name, item in expected.items() if item["config_digest"] == command[-1])
            archive_fixture(Path(command[-2]), mutation="lossy_pair" if corrupt == component else None,
                            component=component)
            return b""
        if command[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": command[-1], "Os": "linux", "Architecture": "arm64"}]).encode()
        if command[0] == "docker":
            return docker(command, data=data)
        raise AssertionError(command)
    monkeypatch.setattr(cli, "prepare_backend", lambda **kw: producer.prepare_backend(**kw, runner=runner))
    monkeypatch.setattr(cli, "prepare_frontend", lambda **kw: producer.prepare_frontend(**kw, runner=runner))
    if corrupt:
        with pytest.raises(producer.PackagingBlocked, match="invalid_image_archive"):
            cli.prepare(args)
        assert not (args.output / "bundle.json").exists()
        assert not (args.output / "manifest.json").exists()
    else:
        assert cli.prepare(args)["status"] == "prepared_not_deployed"
        bundle = json.loads((args.output / "bundle.json").read_bytes())
        assert bundle["version"] == 2
        assert bundle["manifest"]["image"] == expected["backend"]
        assert bundle["frontend"]["image"] == expected["frontend"]
        assert {p.name for p in args.output.iterdir()} == {"backend.tar", "frontend.tar", "bundle.json", "manifest.json"}
        assert sum(call[:2] == ["docker", "build"] for call in calls) == 2
