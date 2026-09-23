"""Unapproved, test-only container artifact; never an approved release build."""
from pathlib import Path

import pytest

from scripts.image_artifact import inspect_archive, resolve_image
from scripts.release_package import run


@pytest.fixture(scope="session")
def unapproved_release_image(tmp_path_factory):
    root = Path(__file__).resolve().parents[2]
    image = run(["docker", "build", "-q", "--platform", "linux/arm64",
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
