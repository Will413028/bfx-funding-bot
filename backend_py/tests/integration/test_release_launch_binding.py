"""Prove explicit hostnames inside real containers; never infer IDs from cgroups."""
import json
import subprocess
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


def test_created_container_receipt_binds_actual_hostname_and_readonly_root():
    image = subprocess.check_output(["docker", "image", "inspect", "python:3.12-alpine",
                                     "--format", "{{.Id}}"], text=True).strip()
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
