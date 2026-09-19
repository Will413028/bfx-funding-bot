"""Cryptographic archives, not caller labels, bind both Docker store ID roles."""
import hashlib
import io
import json
import tarfile

import pytest

from scripts.release_package import PackagingBlocked

OCI = "application/vnd.oci.image."


def archive_fixture(path, mutation=None, component="backend"):
    """Independent OCI bytes plus Docker-save compatibility metadata."""
    members = {}
    def blob(raw, media):
        digest = hashlib.sha256(raw).hexdigest()
        members["blobs/sha256/" + digest] = raw
        return {"digest": "sha256:" + digest, "size": len(raw), "mediaType": OCI + media}
    layer = blob((component + " layer").encode(), "layer.v1.tar")
    config = blob(json.dumps({"os": "linux", "architecture": "arm64", "config": {},
        "rootfs": {"type": "layers", "diff_ids": [layer["digest"]]}}).encode(), "config.v1+json")
    layers = [dict(layer)]
    if mutation == "layer_size":
        layers[0]["size"] += 1
    if mutation == "layer_role":
        layers[0]["mediaType"] = OCI + "config.v1+json"
    manifest = blob(json.dumps({"schemaVersion": 2, "mediaType": OCI + "manifest.v1+json",
        "config": config, "layers": layers}).encode(), "manifest.v1+json")
    descriptor = dict(manifest)
    if mutation == "manifest_size":
        descriptor["size"] += 1
    index = {"schemaVersion": 2, "mediaType": OCI + "index.v1+json", "manifests": [descriptor]}
    if mutation == "double_index":
        index["manifests"].append(descriptor)
    docker = [{"Config": "blobs/sha256/" + config["digest"][7:],
        "RepoTags": None, "Layers": ["blobs/sha256/" + layer["digest"][7:]]}]
    if mutation == "lossy_pair":
        docker.append(dict(docker[0]))
    if mutation == "docker_config":
        docker[0]["Config"] = "blobs/sha256/" + "e" * 64
    members.update({"index.json": json.dumps(index).encode(),
        "manifest.json": json.dumps(docker).encode(), "oci-layout": b'{"imageLayoutVersion":"1.0.0"}'})
    if mutation in {"classic_legacy", "legacy_defaults", "extra_config", "legacy_corrupt", "legacy_wrong_config"}:
        legacy = {"id": "a"*64, "created": "1970-01-01T00:00:00Z",
            "container_config": {}, "config": {}, "os": "linux", "architecture": "arm64"}
        if mutation == "extra_config":
            legacy["rootfs"] = {"type": "layers", "diff_ids": [layer["digest"]]}
        if mutation == "legacy_wrong_config":
            legacy["config"] = {"Cmd": ["wrong"]}
        if mutation == "legacy_defaults":
            legacy["config"] = {"Hostname": "", "Domainname": "", "Image": "",
                "AttachStdin": False, "AttachStdout": False, "AttachStderr": False,
                "Tty": False, "OpenStdin": False, "StdinOnce": False,
                "Volumes": None, "Entrypoint": None, "Labels": None}
        item = blob(json.dumps(legacy).encode(), "config.v1+json")
        if mutation == "legacy_corrupt":
            members["blobs/sha256/" + item["digest"][7:]] = b"broken"
    if mutation in {"config_content", "manifest_content", "layer_content", "missing_layer"}:
        desc = {"config_content": config, "manifest_content": manifest,
                "layer_content": layer, "missing_layer": layer}[mutation]
        name = "blobs/sha256/" + desc["digest"][7:]
        if mutation == "missing_layer":
            del members[name]
        else:
            members[name] = b"x" * len(members[name])
    if mutation == "unsafe":
        members["../escape"] = b"bad"
    if mutation == "duplicate_json":
        members["oci-layout"] = b'{"imageLayoutVersion":"bad","imageLayoutVersion":"1.0.0"}'
    with tarfile.open(path, "w") as tar:
        for name, raw in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
        if mutation == "duplicate_member":
            info = tarfile.TarInfo("index.json")
            info.size = len(members["index.json"])
            tar.addfile(info, io.BytesIO(members["index.json"]))
        if mutation == "symlink":
            info = tarfile.TarInfo("link")
            info.type = tarfile.SYMTYPE
            info.linkname = "index.json"
            tar.addfile(info)
    return {"config_digest": config["digest"], "manifest_digest": manifest["digest"],
            "platform": "linux/arm64"}


@pytest.mark.parametrize("component", ["backend", "frontend"])
def test_archive_verifies_distinct_config_and_manifest(tmp_path, component):
    from scripts.image_artifact import inspect_archive
    path = tmp_path / "image.tar"
    expected = archive_fixture(path, component=component)
    identity = inspect_archive(path)
    assert identity.model_dump() == expected
    assert identity.config_digest != identity.manifest_digest


@pytest.mark.parametrize("mutation", ["lossy_pair", "double_index", "config_content",
    "manifest_content", "layer_content", "missing_layer", "layer_size", "manifest_size",
    "docker_config", "unsafe", "duplicate_json", "duplicate_member", "symlink", "layer_role",
    "extra_config", "legacy_corrupt", "legacy_wrong_config"])
def test_archive_rejects_unproven_or_ambiguous_content(tmp_path, mutation):
    from scripts.image_artifact import inspect_archive
    path = tmp_path / "image.tar"
    archive_fixture(path, mutation)
    with pytest.raises(PackagingBlocked):
        inspect_archive(path)


@pytest.mark.parametrize("mutation", ["classic_legacy", "legacy_defaults"])
def test_classic_save_legacy_layer_json_is_not_an_additional_image(tmp_path, mutation):
    from scripts.image_artifact import inspect_archive
    path = tmp_path / "image.tar"
    expected = archive_fixture(path, mutation)
    assert inspect_archive(path).model_dump() == expected


@pytest.mark.parametrize("store", ["classic", "containerd"])
def test_resolver_uses_inspected_host_id_with_correct_role(tmp_path, store):
    from scripts.image_artifact import ImageNotFound, inspect_archive, resolve_image
    path = tmp_path / "image.tar"
    expected = archive_fixture(path)
    identity = inspect_archive(path)
    host_id = expected["config_digest" if store == "classic" else "manifest_digest"]
    def runner(args, *, data=None):
        assert args[:3] == ["docker", "image", "inspect"]
        if args[-1] != host_id:
            raise ImageNotFound("image_not_found")
        record = {"Id": host_id, "Os": "linux", "Architecture": "arm64"}
        if store == "containerd":
            record["Descriptor"] = {"digest": host_id, "mediaType": OCI + "manifest.v1+json"}
        return json.dumps([record]).encode()
    assert resolve_image(identity, runner) == host_id


@pytest.mark.parametrize("role", ["config_digest", "manifest_digest"])
@pytest.mark.parametrize("bad", [None, "platform", "id", "descriptor", "descriptor_digest", "missing_descriptor"])
def test_resolver_checks_descriptor_for_the_inspected_id_role(tmp_path, role, bad):
    from scripts.image_artifact import ImageNotFound, inspect_archive, resolve_image
    path = tmp_path / "image.tar"
    archive_fixture(path)
    identity = inspect_archive(path)
    host_id = getattr(identity, role)
    lookups = []
    def runner(args, *, data=None):
        assert args[:3] == ["docker", "image", "inspect"]
        lookups.append(args[-1])
        if args[-1] != host_id:
            raise ImageNotFound("image_not_found")
        record = {"Id": host_id, "Os": "linux", "Architecture": "arm64",
            "Descriptor": {"digest": identity.manifest_digest, "mediaType": OCI + "manifest.v1+json"}}
        if bad == "platform":
            record["Architecture"] = "amd64"
        elif bad == "id":
            record["Id"] = "sha256:" + "e" * 64
        elif bad == "descriptor":
            record["Descriptor"]["mediaType"] = OCI + "config.v1+json"
        elif bad == "descriptor_digest":
            record["Descriptor"]["digest"] = identity.config_digest
        elif bad == "missing_descriptor":
            del record["Descriptor"]
        return json.dumps([record]).encode()
    if bad is None or (role == "config_digest" and bad == "missing_descriptor"):
        assert resolve_image(identity, runner) == host_id
    else:
        with pytest.raises(PackagingBlocked, match="image_or_platform_mismatch"):
            resolve_image(identity, runner)
    assert lookups == ([identity.config_digest] if role == "config_digest" else
                       [identity.config_digest, identity.manifest_digest])


@pytest.mark.parametrize("bad", ["error", "json"])
def test_resolver_unclassified_errors_do_not_try_another_role(tmp_path, bad):
    from scripts.image_artifact import inspect_archive, resolve_image
    path = tmp_path / "image.tar"
    archive_fixture(path)
    identity = inspect_archive(path)
    lookups = []
    def runner(args, *, data=None):
        lookups.append(args[-1])
        if bad == "json":
            return b'{"invalid'
        raise PackagingBlocked("command_failed:docker")
    with pytest.raises(PackagingBlocked):
        resolve_image(identity, runner)
    assert lookups == [identity.config_digest]


@pytest.mark.parametrize("bad", ["pax", "trailing_tar"])
def test_archive_rejects_extensions_and_hidden_trailing_metadata(tmp_path, bad):
    from scripts.image_artifact import inspect_archive
    path = tmp_path / "image.tar"
    archive_fixture(path)
    original = path.read_bytes()
    if bad == "trailing_tar":
        path.write_bytes(original + original)
    else:
        header = tarfile.TarInfo("pax")
        header.type = tarfile.XHDTYPE
        header.size = 512
        # A valid extension that tarfile normally silently consumes.
        data = b"19 comment=ignored\n"
        header.size = len(data)
        path.write_bytes(header.tobuf() + data.ljust(512, b"\0") + original)
    with pytest.raises(PackagingBlocked):
        inspect_archive(path)


def test_source_archive_is_cwd_safe_and_repeatable(tmp_path, monkeypatch):
    import subprocess

    from scripts.release_package import source_archive
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args])
    git("init", "-q")
    for component in ("backend_py", "frontend"):
        (tmp_path / component).mkdir()
        (tmp_path / component / "tracked").write_text(component)
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "test: create fixture")
    revision = git("rev-parse", "HEAD").decode().strip()
    timestamp = int(git("show", "-s", "--format=%ct", revision))
    for component in ("backend_py", "frontend"):
        monkeypatch.chdir(tmp_path)
        first = source_archive(revision, component)
        monkeypatch.chdir(tmp_path / "backend_py")
        second = source_archive(revision, component)
        assert first == second
        with tarfile.open(fileobj=io.BytesIO(first)) as tar:
            assert {member.mtime for member in tar} == {timestamp}
            assert tar.getnames() == ["tracked"]
    # Ignore cwd prefix and detect dirt elsewhere in the repository before build.
    (tmp_path / "frontend" / "tracked").write_text("dirty")
    monkeypatch.chdir(tmp_path / "backend_py")
    from scripts.release_package import prepare_backend
    with pytest.raises(PackagingBlocked, match="source_not_clean"):
        prepare_backend(release_id="fixture", platform="linux/arm64",
            config_file=tmp_path / "nonsecret.env", archive_path=tmp_path / "backend.tar")
