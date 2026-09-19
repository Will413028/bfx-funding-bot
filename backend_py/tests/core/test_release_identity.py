"""Runtime evidence must be measured, and launch identity must not be reusable."""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from bfx_funding_bot.core import release_identity as ri

_real_protection_check = ri.assert_protected_file


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.fixture
def artifact(tmp_path: Path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    for name in ri.INVENTORY_ROOTS:
        path = root / name
        path.mkdir(parents=True)
        (path / "fixture.txt").write_text(name)
    for name in ri.INVENTORY_FILES:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(name)
    python_prefix = tmp_path / "python"
    (python_prefix / "bin").mkdir(parents=True)
    (python_prefix / "lib/python3.13").mkdir(parents=True)
    (python_prefix / "bin/python3.13").write_bytes(b"fixture python")
    (python_prefix / "lib/libpython3.13.so.1.0").write_bytes(b"fixture libpython")
    manifest = {
        "version": 1, "release_id": "release-test", "source_revision": "a" * 40,
        "platform": "linux/arm64", "docker_image_id": "sha256:" + "b" * 64,
        "oci_manifest_digest": None, "inventory": ri.measure_inventory(root),
        "python_inventory": ri.measure_python_inventory(python_prefix),
        "environment": {"BFX_PHASE": "live", "BFX_EXECUTOR": "bitfinex_live"},
        "schema_head": "test-head", "projector_version": "test-projector",
    }
    launch_id = uuid4().hex
    launch = {
        "version": 1, "launch_id": launch_id, "hostname": "bfx-" + launch_id,
        "container_id": "c" * 64, "manifest_digest": digest(manifest),
        "docker_image_id": manifest["docker_image_id"], "platform": "linux/arm64",
    }
    manifest_path = tmp_path / "manifest.json"
    receipt_path = tmp_path / "launch.json"
    manifest_path.write_text(json.dumps(manifest))
    receipt_path.write_text(json.dumps(launch))
    monkeypatch.setattr(ri, "assert_protected_file", lambda path: None)
    runtime = ri.ReleaseRuntime(
        root=root, manifest_path=manifest_path, receipt_path=receipt_path,
        python_prefix=python_prefix,
        hostname=lambda: launch["hostname"], platform=lambda: "linux/arm64",
        environment=lambda: dict(manifest["environment"]),
    )
    return runtime, root, manifest_path, receipt_path, manifest, launch


def test_interpreter_and_standard_library_are_measured(tmp_path):
    prefix = tmp_path / "python"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "lib/python3.13").mkdir(parents=True)
    (prefix / "bin/python3.13").write_bytes(b"fixture interpreter")
    (prefix / "lib/libpython3.13.so.1.0").write_bytes(b"fixture runtime")
    (prefix / "lib/python3.13/site.py").write_bytes(b"fixture stdlib")
    first = ri.measure_python_inventory(prefix)
    (prefix / "bin/python3.13").write_bytes(b"mutated interpreter")
    second = ri.measure_python_inventory(prefix)
    assert first != second
    assert set(first) == {"bin/python3.13", "lib/libpython3.13.so.1.0", "lib/python3.13/site.py"}


def test_measured_release_survives_fresh_container_launch(artifact):
    runtime, _, _, receipt_path, _, launch = artifact
    first = runtime.verify()
    launch["launch_id"] = uuid4().hex
    launch["hostname"] = "bfx-" + launch["launch_id"]
    launch["container_id"] = "d" * 64
    receipt_path.write_text(json.dumps(launch))
    second = runtime.verify()
    assert first.release_digest == second.release_digest
    assert first.launch_id != second.launch_id


def test_amount_rule_revision_changes_config_identity_but_fx_does_not(artifact, monkeypatch):
    from dataclasses import replace

    from tests.external.bitfinex.test_funding_rules import evidence
    runtime, *_ = artifact
    first = runtime.verify()
    # Market observations never become release configuration.
    assert evidence(rate="1").payload() != evidence(rate="0.5").payload()
    assert runtime.verify().config_digest == first.config_digest
    monkeypatch.setattr(ri, "RULE", replace(ri.RULE, version="future-rule"))
    assert runtime.verify().config_digest != first.config_digest


@pytest.mark.parametrize("inventory_path", ["src/fixture.txt", "scripts/fixture.txt",
    "alembic/fixture.txt", "configs/fixture.txt", ".venv/lib/python3.13/site-packages/fixture.txt",
    "uv.lock", "pyproject.toml"])
def test_changed_runtime_content_blocks(artifact, inventory_path):
    runtime, root, *_ = artifact
    runtime.verify()
    (root / inventory_path).write_text("changed")
    with pytest.raises(ri.ReleaseIdentityError, match="inventory"):
        runtime.verify()


def test_extra_executable_is_not_hidden_by_manifest(artifact):
    runtime, root, *_ = artifact
    (root / "src/new.py").write_text("raise RuntimeError")
    with pytest.raises(ri.ReleaseIdentityError, match="inventory"):
        runtime.verify()


def test_old_receipt_on_new_container_blocks(artifact):
    runtime, *_ = artifact
    runtime.hostname = lambda: "bfx-" + uuid4().hex
    with pytest.raises(ri.ReleaseIdentityError, match="launch"):
        runtime.verify()


def test_environment_change_blocks_and_secrets_are_not_manifest_inputs(artifact):
    runtime, *_ = artifact
    runtime.environment = lambda: {"BFX_PHASE": "live", "BFX_EXECUTOR": "paper"}
    with pytest.raises(ri.ReleaseIdentityError, match="environment"):
        runtime.verify()
    assert ri.runtime_environment({"BFX_API_KEY": "test-secret", "BFX_PHASE": "live"}) == {
        "BFX_PHASE": "live",
    }


def test_wrong_image_kind_and_missing_receipt_block(artifact):
    runtime, _, _, receipt_path, _, launch = artifact
    launch["docker_image_id"] = "sha256:" + "e" * 64
    receipt_path.write_text(json.dumps(launch))
    with pytest.raises(ri.ReleaseIdentityError, match="launch"):
        runtime.verify()
    receipt_path.unlink()
    with pytest.raises(ri.ReleaseIdentityError, match="unavailable"):
        runtime.verify()


def test_matching_inventory_on_writable_filesystem_is_not_execution_authority(artifact, monkeypatch):
    runtime, _root, manifest_path, receipt_path, *_ = artifact
    # Only the receipt mount is simulated. The actual temporary code filesystem
    # is writable and must fail even when all content hashes match.
    def protection(path):
        if path in {manifest_path, receipt_path}:
            return
        _real_protection_check(path)
    monkeypatch.setattr(ri, "assert_protected_file", protection)
    with pytest.raises(ri.ReleaseIdentityError, match=r"unprotected|writable"):
        runtime.verify()


def test_unclassified_secret_is_rejected_without_copying_value():
    secret = "synthetic-do-not-copy-unknown-secret"
    with pytest.raises(ri.ReleaseIdentityError) as error:
        ri.runtime_environment({"BFX_PHASE": "live", "BFX_NEW_SECRET": secret})
    assert "BFX_NEW_SECRET" in str(error.value)
    assert secret not in str(error.value)
    assert ri.runtime_environment({"BFX_API_SECRET": secret, "BFX_ADMIN_TOKEN": secret,
                                   "BFX_VAULT_KEK": secret, "BFX_PHASE": "live"}) == {"BFX_PHASE": "live"}


def test_configuration_outside_measured_inventory_is_rejected(artifact):
    runtime, root, manifest_path, receipt_path, manifest, receipt = artifact
    manifest["environment"]["BFX_SAFETY_CONFIG"] = str(root.parent / "unmeasured.yaml")
    manifest_path.write_text(json.dumps(manifest))
    receipt["manifest_digest"] = digest(manifest)
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ri.ReleaseIdentityError, match="config"):
        runtime.verify()


def test_python_entrypoint_is_part_of_executable_inventory(artifact):
    runtime, root, *_ = artifact
    path = root / ".venv/bin/bfx-shadow"
    path.parent.mkdir(exist_ok=True)
    path.write_text("modified console entrypoint")
    with pytest.raises(ri.ReleaseIdentityError, match="inventory"):
        runtime.verify()


def test_module_launch_rejects_uninventoried_dotenv_override(tmp_path, monkeypatch):
    import sys
    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(ri, "__file__", str(root / "src/bfx_funding_bot/core/release_identity.py"))
    monkeypatch.setattr(sys, "base_prefix", "/usr/local")
    monkeypatch.setattr(sys, "prefix", str(root / ".venv"))
    monkeypatch.setattr(sys, "executable", "/usr/local/bin/python3.13")
    monkeypatch.setattr(sys, "argv", [str(root / "src/bfx_funding_bot/modules/marketfeed/daemon.py")])
    monkeypatch.setattr(ri.os, "getuid", lambda: 1000)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "LD_PRELOAD", "LD_LIBRARY_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BFX_RELEASE_MANIFEST_PATH", "/run/bfx-release/manifest.json")
    monkeypatch.setenv("BFX_RELEASE_LAUNCH_RECEIPT_PATH", "/run/bfx-release/launch.json")
    assert ri.ReleaseRuntime.from_environment().root == root
    (root / ".env").write_text("BFX_OPERATOR_USER_ID=uninventoried-override")
    with pytest.raises(ri.ReleaseIdentityError, match="environment"):
        ri.ReleaseRuntime.from_environment()
