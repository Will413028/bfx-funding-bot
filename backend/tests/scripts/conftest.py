"""Unapproved, test-only container artifact; never an approved release build."""
from pathlib import Path

import pytest

from scripts.image_artifact import inspect_archive, resolve_image
from scripts.release_package import run


def _host_platform() -> str:
    """The daemon's native platform, so the fixture builds without emulation.

    Production is linux/arm64, but these tests exercise packaging and launch
    identity, not the CPU: an x86 CI runner cannot build or run arm64 without
    QEMU, and an Apple Silicon host still builds arm64 here.
    """
    arch = run(["docker", "info", "--format", "{{.Architecture}}"]).decode().strip()
    return {"x86_64": "linux/amd64", "aarch64": "linux/arm64", "arm64": "linux/arm64"}[arch]


@pytest.fixture(scope="session")
def unapproved_release_image(tmp_path_factory):
    root = Path(__file__).resolve().parents[2]
    image = run(["docker", "build", "-q", "--platform", _host_platform(),
                 "--build-arg", "GIT_SHA=unapproved-portability-fixture", str(root)]).decode().strip()
    directory = tmp_path_factory.mktemp("unapproved-image")
    path = directory / "backend.tar"
    run(["docker", "save", "--output", str(path), image])
    try:
        identity = inspect_archive(path)
        assert resolve_image(identity, run) == image
        print("unapproved_fixture_identity=" + identity.model_dump_json())
        yield image, identity
    finally:
        path.unlink(missing_ok=True)
