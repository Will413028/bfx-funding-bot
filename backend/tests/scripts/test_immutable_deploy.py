"""Orchestrator acceptance; external Docker/host-root boundaries are fixtures."""
import json
from types import SimpleNamespace

import pytest

from tests.scripts.test_release_package import IDENTITY, IMAGE, manifest


def fixture(tmp_path, monkeypatch):
    from scripts import immutable_release as cli
    release = manifest().model_copy(update={"environment": {"BFX_PHASE": "live",
        "BFX_OPERATOR_USER_ID": "operator", "BFX_OPERATOR_ROLE": "admin",
        "BFX_EXCHANGE_ACCOUNT_ID": "account", "BFX_DEPLOYMENT_ENV": "prod",
        "BFX_SAFETY_CONFIG": "/app/configs/safety.live.yaml"}})
    args = SimpleNamespace(bundle=tmp_path / "bundle.json", network="existing", halt2=tmp_path / "halt2.json",
        bot_env=tmp_path / "bot.env", webapi_env=tmp_path / "api.env", frontend_env=tmp_path / "frontend.env",
        dr_directory=tmp_path / "dr")
    public = {"NEXT_PUBLIC_APP_URL": "https://example.test"}
    for path, role in [(args.bot_env, "bfx_bot"), (args.webapi_env, "bfx_webapi"), (args.frontend_env, "bfx_webauth")]:
        path.write_text(f"DATABASE_URL=postgresql://{role}:synthetic@bfx-postgres/bfx\nBFX_ADMIN_TOKEN=synthetic\n"
            "BFX_OPERATOR_USER_ID=operator\nBFX_OPERATOR_ROLE=admin\nNEXT_PUBLIC_APP_URL=https://example.test\n")
    with args.webapi_env.open("a") as stream:
        stream.write("BFX_DEPLOYMENT_ENV=prod\n")
    args.halt2.write_text(json.dumps({"backup_evidence_path": "/run/bfx-dr/backup.json",
        "isolated_restore_evidence_path": "/run/bfx-dr/restore.json"}))
    args.dr_directory.mkdir()
    for name in ("backup", "restore"):
        (args.dr_directory / f"{name}.json").write_text('{}')
    # This fixture is not host-root acceptance. Actual protected files/RO rootfs
    # and shared consumer verification run separately inside the candidate image.
    monkeypatch.setattr(cli, "protected", lambda *a, **k: None)
    monkeypatch.setattr(cli.os, "chown", lambda *a: None)
    calls = []
    infra = [{"Id": "existing-pg", "State": {"Running": True}, "Mounts": [{"Name": "bfx_pgdata"}]},
             {"Id": "existing-redis", "State": {"Running": True}, "Mounts": [{"Name": "bfx_redisdata"}]}]
    def runner(command, *, data=None):
        calls.append(command)
        if command == ["docker", "inspect", "bfx-postgres", "bfx-redis"]:
            return json.dumps(infra).encode()
        if command == ["docker", "inspect", "bfx-bot", "bfx-webapi", "bfx-frontend"]:
            return json.dumps([{"Id": n, "Name": "/"+n, "State": {"Running": False}} for n in command[2:]]).encode()
        if command[:2] in (["docker", "rename"], ["docker", "network"]):
            return b""
        raise AssertionError(command)
    monkeypatch.setattr(cli, "run", runner)
    checks = []
    def one_shot(*a, **k):
        checks.append(k)
        return b'{"halt_id":17,"halted":true,"policies":{"fUST":{"revision":1}}}'
    monkeypatch.setattr(cli, "one_shot", one_shot)
    launches = []
    def launch(release, **kwargs):
        from bfx_funding_bot.core.release_identity import LaunchReceipt
        launches.append((release.image, kwargs))
        kwargs["publish"](LaunchReceipt(version=2, launch_id="a"*32, hostname="bfx-"+"a"*32,
            container_id="b"*64, manifest_digest="c"*64, image=IDENTITY,
            actual_image_id=IMAGE, platform="linux/arm64"))
        return "new-bot"
    monkeypatch.setattr(cli, "create_launch", launch)
    monkeypatch.setattr(cli, "start_service", lambda **kw: launches.append((kw["identity"],kw)) or kw["name"])
    bundle = {"frontend": {"image": IDENTITY.model_dump(), "public_environment": public}}
    return cli, args, bundle, release, calls, checks, launches


def test_deploy_preserves_infrastructure_and_halt_and_publishes_dr_before_start(tmp_path, monkeypatch):
    cli, args, bundle, release, calls, checks, launches = fixture(tmp_path, monkeypatch)
    result = cli.deploy(args, bundle, release)
    assert result["resumed"] is False
    assert result["halt_before"]["halt_id"] == result["halt_after"]["halt_id"] == 17
    assert result["human_activation_required"] is True
    assert [image for image, _ in launches] == [IDENTITY, IDENTITY, IDENTITY]
    evidence = launches[0][1]["evidence_mounts"]
    assert set(evidence) == {"/run/bfx-dr/backup.json", "/run/bfx-dr/restore.json"}
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in evidence.values())
    assert all(not any(word in cmd for word in ("build", "pull", "up", "rm", "stop")) for cmd in calls)
    assert sum(check["command"][-1] == "startup" for check in checks) == 2


def test_missing_policy_stops_before_renaming_or_starting_apps(tmp_path, monkeypatch):
    cli, args, bundle, release, calls, _, launches = fixture(tmp_path, monkeypatch)
    def blocked(*a, **k):
        raise cli.PackagingBlocked("deployment_database_check_failed")
    monkeypatch.setattr(cli, "one_shot", blocked)
    with pytest.raises(cli.PackagingBlocked):
        cli.deploy(args, bundle, release)
    assert not launches
    assert not any(cmd[:2] == ["docker", "rename"] for cmd in calls)


@pytest.mark.parametrize("drift", [None, "Id", "Source", "Destination", "RW", "missing", "duplicate"])
def test_deploy_mount_order_is_not_drift_but_mount_changes_are(tmp_path, monkeypatch, drift):
    cli, args, bundle, release, _, _, _ = fixture(tmp_path, monkeypatch)
    original_runner = cli.run
    inspections = 0

    def runner(command, *, data=None):
        nonlocal inspections
        raw = original_runner(command, data=data)
        if command != ["docker", "inspect", "bfx-postgres", "bfx-redis"]:
            return raw
        inspections += 1
        rows = json.loads(raw)
        mounts = [
            {"Type": "volume", "Name": "bfx_pgdata", "Source": "/volumes/pg", "Destination": "/var/lib/postgresql", "RW": True},
            {"Type": "bind", "Source": "/config/pg.conf", "Destination": "/etc/pg.conf", "RW": False},
        ]
        if inspections == 2:
            if drift == "Id":
                rows[0]["Id"] = "recreated-pg"
            elif drift in {"Source", "Destination", "RW"}:
                mounts[0][drift] = False if drift == "RW" else "/different"
            elif drift == "missing":
                mounts.pop()
            elif drift == "duplicate":
                mounts.append(dict(mounts[0]))
            mounts.reverse()
        rows[0]["Mounts"] = mounts
        return json.dumps(rows).encode()

    monkeypatch.setattr(cli, "run", runner)
    if drift:
        with pytest.raises(cli.PackagingBlocked, match="infrastructure_changed"):
            cli.deploy(args, bundle, release)
        assert not list(tmp_path.glob("launch-*/deployment.json"))
    else:
        result = cli.deploy(args, bundle, release)
        assert result["resumed"] is False
        receipts = list(tmp_path.glob("launch-*/deployment.json"))
        assert len(receipts) == 1
        assert json.loads(receipts[0].read_text()) == result


@pytest.mark.parametrize("realm", [None, "ci"])
def test_webapi_realm_missing_or_mismatched_blocks_before_rename_or_start(tmp_path, monkeypatch, realm):
    cli, args, bundle, release, calls, checks, launches = fixture(tmp_path, monkeypatch)
    env = args.webapi_env.read_text().replace("BFX_DEPLOYMENT_ENV=prod\n", "")
    args.webapi_env.write_text(env + (f"BFX_DEPLOYMENT_ENV={realm}\n" if realm else ""))
    with pytest.raises(cli.PackagingBlocked, match="runtime_environment_or_operator_mismatch"):
        cli.deploy(args, bundle, release)
    assert not launches
    assert not checks
    assert not calls


def test_dr_destination_cannot_override_application_or_root_receipt(tmp_path, monkeypatch):
    cli, args, bundle, release, _, _, launches = fixture(tmp_path, monkeypatch)
    args.halt2.write_text(json.dumps({"backup_evidence_path": "/app/configs/safety.live.yaml",
        "isolated_restore_evidence_path": "/run/bfx-dr/restore.json"}))
    with pytest.raises(cli.PackagingBlocked, match="evidence"):
        cli.deploy(args, bundle, release)
    assert not launches


@pytest.mark.parametrize("store", ["classic", "containerd"])
@pytest.mark.parametrize("drift", [None, "environment", "privileged", "network", "image"])
def test_service_inspection_rejects_config_drift_before_start(tmp_path, monkeypatch, drift, store):
    from scripts import immutable_release as cli
    from scripts.image_artifact import ImageNotFound
    image = IMAGE if store == "classic" else IDENTITY.manifest_digest
    env = tmp_path / "api.env"
    env.write_text("BFX_OPERATOR_USER_ID=operator\n")
    calls = []
    def runner(command, *, data=None):
        calls.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            if command[-1] != image:
                raise ImageNotFound("image_not_found")
            record = {"Id": image, "Os": "linux", "Architecture": "arm64"}
            if store == "containerd":
                record["Descriptor"] = {"digest": image, "mediaType": "application/vnd.oci.image.manifest.v1+json"}
            return json.dumps([record]).encode()
        if command[:2] == ["docker", "create"]:
            assert image in command
            return b"c" * 64
        if command[:2] == ["docker", "inspect"]:
            return json.dumps([{"Id": "c"*64, "Image": image if drift != "image" else "sha256:"+"e"*64, "Platform": "linux",
                "State": {"Running": False}, "Mounts": [],
                "Config": {"Cmd": ["python", "-m", "uvicorn"], "User": "1000:1000", "Entrypoint": None,
                    "WorkingDir": "/app", "Env": ["BFX_OPERATOR_USER_ID=" + ("other" if drift == "environment" else "operator")]},
                "HostConfig": {"ReadonlyRootfs": True, "CapAdd": None, "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges"], "Privileged": drift == "privileged",
                    "NetworkMode": "other" if drift == "network" else "existing"}}]).encode()
        if command[:2] == ["docker", "start"]:
            return b""
        raise AssertionError(command)
    monkeypatch.setattr(cli, "run", runner)
    kwargs = {"identity": IDENTITY, "name": "fixture", "user": "1000:1000", "env": env,
        "network": "existing", "command": ["python", "-m", "uvicorn"]}
    if drift:
        with pytest.raises(cli.PackagingBlocked, match="service_launch_mismatch"):
            cli.start_service(**kwargs)
        assert not any(cmd[:2] == ["docker", "start"] for cmd in calls)
    else:
        assert cli.start_service(**kwargs) == "c"*64


def test_cli_existing_receipt_blocks_before_any_one_shot(tmp_path, monkeypatch, capsys):
    from scripts import immutable_release as cli
    receipt = tmp_path / "existing.json"
    receipt.write_text('{"preserved":true}')
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli.sys, "argv", ["release", "bootstrap", "--bundle", "/bundle.json",
        "--network", "fixture", "--env", "/fixture.env", "--receipt", str(receipt)])
    release = manifest().model_copy(update={"environment": {"BFX_EXCHANGE_ACCOUNT_ID": "account", "BFX_DEPLOYMENT_ENV": "ci"}})
    monkeypatch.setattr(cli, "read_bundle", lambda _: ({}, release))
    calls = []
    monkeypatch.setattr(cli, "one_shot", lambda *a, **k: calls.append(k) or b'{}')
    assert cli.main() == 2
    assert not calls
    assert receipt.read_text() == '{"preserved":true}'
    assert "new_receipt_path_required" in capsys.readouterr().err


@pytest.mark.parametrize("drift", ["source", "platform", "backend_image"])
def test_bundle_rejects_inconsistent_provenance(tmp_path, monkeypatch, drift):
    from bfx_funding_bot.core.release_identity import canonical_digest
    from scripts import immutable_release as cli
    release = manifest()
    backend = {"manifest_digest": canonical_digest(release.model_dump(mode="json")),
        "source_revision": release.source_revision, "image": IDENTITY.model_dump()}
    frontend = {"source_revision": release.source_revision, "image": IDENTITY.model_dump()}
    if drift == "source":
        frontend["source_revision"] = "d"*40
    elif drift == "platform":
        frontend["image"]["platform"] = "linux/amd64"
    else:
        backend["image"]["config_digest"] = "sha256:" + "d"*64
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps({"version": 2, "manifest": release.model_dump(mode="json"),
        "backend_preparation": backend, "frontend": frontend}))
    monkeypatch.setattr(cli, "protected", lambda *a, **k: None)
    with pytest.raises(cli.PackagingBlocked, match="preparation_mismatch"):
        cli.read_bundle(path)


@pytest.mark.parametrize("bad", [None, "v1", "digest", "config", "manifest", "path", "platform"])
def test_bundle_verifies_both_archives_before_host_execution(tmp_path, monkeypatch, bad):
    import hashlib

    from bfx_funding_bot.core.release_identity import PackagedImageIdentity, canonical_digest
    from scripts import immutable_release as cli
    from tests.scripts.test_image_artifact import archive_fixture
    artifacts = {}
    for component in ("backend", "frontend"):
        path = tmp_path / (component + ".tar")
        image = archive_fixture(path, component=component)
        artifacts[component] = {"image": image, "archive_filename": path.name,
            "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_revision": "b"*40}
    release = manifest().model_copy(update={
        "image": PackagedImageIdentity.model_validate(artifacts["backend"]["image"])})
    artifacts["backend"]["manifest_digest"] = canonical_digest(release.model_dump(mode="json"))
    bundle = {"version": 2, "manifest": release.model_dump(mode="json"),
        "backend_preparation": artifacts["backend"], "frontend": artifacts["frontend"]}
    if bad == "v1":
        bundle["version"] = 1
    elif bad == "digest":
        artifacts["frontend"]["archive_sha256"] = "e"*64
    elif bad in {"config", "manifest"}:
        artifacts["frontend"]["image"][bad + "_digest"] = "sha256:" + "e"*64
    elif bad == "path":
        artifacts["frontend"]["archive_filename"] = "../frontend.tar"
    elif bad == "platform":
        artifacts["frontend"]["image"]["platform"] = "linux/amd64"
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle))
    monkeypatch.setattr(cli, "protected", lambda *a, **k: None)
    calls = []
    def runner(args, *, data=None):
        calls.append(args)
        assert args[:3] == ["docker", "image", "inspect"]
        return json.dumps([{"Id": args[-1], "Os": "linux", "Architecture": "arm64"}]).encode()
    monkeypatch.setattr(cli, "run", runner)
    if bad:
        with pytest.raises(cli.PackagingBlocked):
            cli.read_bundle(path)
        assert calls == []
    else:
        assert cli.read_bundle(path)[1] == release
        assert [call[-1] for call in calls] == [
            artifacts["backend"]["image"]["config_digest"],
            artifacts["frontend"]["image"]["config_digest"]]


@pytest.mark.parametrize("store", ["classic", "containerd"])
@pytest.mark.parametrize("mutation", [None, "Image", "Id", "Command", "ReadonlyRootfs",
    "NetworkMode", "Env", "Mounts", "Tmpfs", "start_error"])
def test_one_shot_inspects_before_start_and_cleans_up(tmp_path, monkeypatch, store, mutation):
    from scripts import immutable_release as cli
    from tests.scripts.test_release_package import OneShotDocker
    image = IMAGE if store == "classic" else IDENTITY.manifest_digest
    env = tmp_path / "env"
    env.write_text("BFX_PHASE=live\n")
    monkeypatch.setattr(cli, "protected", lambda *a, **k: None)
    for command in (["uv", "run", "alembic", "upgrade", "head"],
                    ["python", "-m", "scripts.bootstrap_capital"],
                    ["python", "-m", "scripts.convert_capital_policy"],
                    ["python", "-m", "scripts.release_database", "startup"]):
        docker = OneShotDocker(image=image, mutation=mutation)
        monkeypatch.setattr(cli, "run", docker)
        if mutation:
            with pytest.raises(cli.PackagingBlocked):
                cli.one_shot(manifest(), env=env, network="fixture", command=command)
            assert docker.started == (mutation == "start_error")
        else:
            assert cli.one_shot(manifest(), env=env, network="fixture", command=command,
                extra_env={"UV_NO_SYNC": "1"},
                mounts=[f"type=bind,src={env},dst=/run/fixture.env,readonly"]) == b"verified"
            assert docker.calls.index(["docker", "inspect", docker.cid]) < docker.calls.index(
                ["docker", "start", "--attach", docker.cid])
        assert docker.calls[-1] == ["docker", "rm", "--force", "--volumes", docker.cid]


@pytest.mark.parametrize("start_error", [False, True])
def test_one_shot_checks_process_exit_independently_of_attach_status(tmp_path, monkeypatch, start_error):
    from scripts import immutable_release as cli
    from tests.scripts.test_release_package import OneShotDocker
    env = tmp_path / "env"
    env.write_text("BFX_PHASE=live\n")
    docker = OneShotDocker(exit_code=17, mutation="start_error" if start_error else None)
    monkeypatch.setattr(cli, "protected", lambda *a, **k: None)
    monkeypatch.setattr(cli, "run", docker)
    with pytest.raises(cli.PackagingBlocked, match="one_shot_exit_nonzero:17"):
        cli.one_shot(manifest(), env=env, network="fixture", command=["python", "-c", "raise SystemExit(17)"])
    assert docker.calls[-1] == ["docker", "rm", "--force", "--volumes", docker.cid]
