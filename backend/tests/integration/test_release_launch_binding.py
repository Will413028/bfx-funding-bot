"""Prove explicit hostnames inside real containers; never infer IDs from cgroups."""
import json
import subprocess
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration

_IMAGE = "python:3.12-alpine"


def _local_image_id(reference: str) -> str:
    """Resolve a local image ID, pulling it first on a runner that lacks it.

    The test binds receipts to the ID of an image already on the host, as a real
    launch does; any small image with python will do, so fetch it like the
    testcontainers images rather than fail on a fresh CI runner.
    """
    inspect = ["docker", "image", "inspect", reference, "--format", "{{.Id}}"]
    if subprocess.run(inspect, capture_output=True, check=False).returncode != 0:
        subprocess.run(["docker", "pull", "--quiet", reference], check=True, capture_output=True)
    return subprocess.check_output(inspect, text=True).strip()


def test_created_container_receipt_binds_actual_hostname_and_readonly_root():
    image = _local_image_id(_IMAGE)
    identities = []
    for _ in range(2):
        hostname = "bfx-" + uuid4().hex
        container = subprocess.check_output([
            "docker", "create", "--network", "none", "--read-only", "--hostname", hostname,
            image, "python", "-c", "import os,socket,json; print(json.dumps({'hostname':socket.gethostname(),'readonly':bool(os.statvfs('/').f_flag & os.ST_RDONLY),'uid':os.stat('/usr/local/bin').st_uid}))",
        ], text=True).strip()
        try:
            inspected = json.loads(subprocess.check_output(["docker", "inspect", container], text=True))[0]
            assert inspected["Image"] == image
            assert inspected["Config"]["Hostname"] == hostname
            observed = json.loads(subprocess.check_output(["docker", "start", "--attach", container], text=True))
            assert observed == {"hostname": hostname, "readonly": True, "uid": 0}
            identities.append((inspected["Id"], observed["hostname"]))
        finally:
            subprocess.run(["docker", "rm", "-f", container], check=True, capture_output=True)
    assert identities[0][0] != identities[1][0]
    assert identities[0][1] != identities[1][1]
