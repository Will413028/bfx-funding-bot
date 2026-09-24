"""Unapproved, test-only container artifact; never an approved release build."""
import json
import tarfile
from pathlib import Path

import pytest

from scripts.image_artifact import PackagingBlocked, inspect_archive, resolve_image
from scripts.release_package import run


def _host_platform() -> str:
    """The daemon's native platform, so the fixture builds without emulation.

    Production is linux/arm64, but these tests exercise packaging and launch
    identity, not the CPU: an x86 CI runner cannot build or run arm64 without
    QEMU, and an Apple Silicon host still builds arm64 here.
    """
    arch = run(["docker", "info", "--format", "{{.Architecture}}"]).decode().strip()
    return {"x86_64": "linux/amd64", "aarch64": "linux/arm64", "arm64": "linux/arm64"}[arch]


def _describe_rejected_archive(path: Path) -> None:
    """Print the rejected archive's shape; the verifier deliberately hides why.

    Diagnostic only: the fixture still fails. Shows the daemon's image store and
    the tar's index/manifest documents so a runner-specific rejection can be read
    from CI output instead of reproduced blind.
    """
    store = run(["docker", "info", "--format", "{{.Driver}} {{json .DriverStatus}}"])
    print("rejected_archive_docker_store=" + store.decode().strip())
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        print("rejected_archive_members=" + json.dumps(
            [(m.name, m.size) for m in members if not m.name.startswith("blobs/")
             or m.size < 4096][:60]))
        for name in ("index.json", "manifest.json", "oci-layout"):
            member = next((m for m in members if m.name == name), None)
            if member is not None and (stream := archive.extractfile(member)) is not None:
                print(f"rejected_archive_{name}=" + stream.read(4000).decode(errors="replace"))
        index = next((m for m in members if m.name == "index.json"), None)
        if index is not None and (stream := archive.extractfile(index)) is not None:
            for descriptor in json.loads(stream.read()).get("manifests", []):
                blob = "blobs/" + descriptor["digest"].replace(":", "/")
                nested = next((m for m in members if m.name == blob), None)
                if nested is not None and (body := archive.extractfile(nested)) is not None:
                    print(f"rejected_archive_blob[{descriptor['digest'][:19]}]="
                          + body.read(4000).decode(errors="replace"))


@pytest.fixture(scope="session")
def unapproved_release_image(tmp_path_factory):
    root = Path(__file__).resolve().parents[2]
    image = run(["docker", "build", "-q", "--platform", _host_platform(),
                 "--build-arg", "GIT_SHA=unapproved-portability-fixture", str(root)]).decode().strip()
    directory = tmp_path_factory.mktemp("unapproved-image")
    path = directory / "backend.tar"
    run(["docker", "save", "--output", str(path), image])
    try:
        try:
            identity = inspect_archive(path)
        except PackagingBlocked:
            _describe_rejected_archive(path)
            raise
        assert resolve_image(identity, run) == image
        print("unapproved_fixture_identity=" + identity.model_dump_json())
        yield image, identity
    finally:
        path.unlink(missing_ok=True)
