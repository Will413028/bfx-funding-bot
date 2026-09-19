"""Measured runtime identity under a trusted, host-controlled launch receipt.

The host creates each container with hostname ``bfx-<fresh launch UUID hex>``
and records Docker's inspected ID/Image/Hostname before starting it. No cgroup
ID inference or Docker access occurs here. Host root compromise is out of scope.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform as platform_module
import socket
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from bfx_funding_bot.external.bitfinex.funding_rules import RULE

INVENTORY_ROOTS = ("src", "scripts", "alembic", "configs",
                   ".venv/lib/python3.13/site-packages")
INVENTORY_FILES = ("uv.lock", "pyproject.toml", "alembic.ini", ".venv/bin/bfx-shadow", ".venv/pyvenv.cfg")
_SECRET_ENV = frozenset({"BFX_API_KEY", "BFX_API_SECRET", "BFX_ADMIN_TOKEN", "BFX_VAULT_KEK"})
_PATH_ENV = frozenset({"BFX_RELEASE_MANIFEST_PATH", "BFX_RELEASE_LAUNCH_RECEIPT_PATH"})
NONSECRET_ENV_KEYS = frozenset({
    "BFX_BOOK_MAX_AGE_SECONDS", "BFX_BOOK_MAX_DOWN_PCT", "BFX_BOOK_RECONCILE_INTERVAL_SECONDS",
    "BFX_BOOK_SNAPSHOT_ENABLED", "BFX_BOOK_SNAPSHOT_INTERVAL_S", "BFX_CELLS_YAML", "BFX_CELLS",
    "BFX_DEPLOYMENT_ENV", "BFX_EXCHANGE_ACCOUNT_ID", "BFX_EXECUTION_POLICY", "BFX_EXECUTOR",
    "BFX_FILL_MODEL_ARTIFACT", "BFX_FILL_TRACKER_ENABLED", "BFX_HALT2_EVIDENCE_REPORT",
    "BFX_HALT2_MAX_SNAPSHOT_AGE_SECONDS", "BFX_HEALTHZ_HOST", "BFX_HEALTHZ_PORT",
    "BFX_KILL_SWITCH", "BFX_LADDER_MIN_RUNG_USDT", "BFX_LADDER_MULTIPLIERS",
    "BFX_LADDER_OBSERVE", "BFX_LADDER_SPIKE_FRACTION",
    "BFX_OPERATOR_ROLE", "BFX_OPERATOR_USER_ID", "BFX_OPTIMIZER_FEE_RATE",
    "BFX_OTEL_ENABLED", "BFX_OTEL_EXPORTER_ENDPOINT", "BFX_PHASE", "BFX_PROJECTOR_VERSION",
    "BFX_PUBLIC_EXCHANGE_ACCOUNT_ID", "BFX_QUOTE_TTL_MS", "BFX_RATE_LIMIT_READ_PER_MIN",
    "BFX_RATE_LIMIT_WRITE_PER_MIN", "BFX_READINESS_TIMEOUT_SECONDS", "BFX_RECONCILE_INTERVAL_S",
    "BFX_REPRICE_ENABLED", "BFX_REPRICE_MAX_CANCELS_PER_TICK", "BFX_REPRICE_MIN_AGE_S",
    "BFX_REPRICE_TOLERANCE_PCT", "BFX_RESYNC_MIN_INTERVAL_S", "BFX_RUN_DURATION_HOURS",
    "BFX_SAFETY_CONFIG", "BFX_SCHEDULER_BUFFER_S", "BFX_SERVICE_VERSION",
    "BFX_STALENESS_BUDGET_HOURS_DEFAULT", "BFX_WS_CLIENT_ENABLED",
})
_SHA = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_IMAGE = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class ReleaseIdentityError(RuntimeError):
    pass


class PackagedImageIdentity(BaseModel):
    """Content roles remain stable even when Docker's engine ID changes."""
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    config_digest: _IMAGE
    manifest_digest: _IMAGE
    platform: Literal["linux/arm64", "linux/amd64"]


class ReleaseManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[2]
    release_id: str = Field(min_length=1)
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    image: PackagedImageIdentity
    inventory: dict[str, _SHA]
    python_inventory: dict[str, _SHA]
    environment: dict[str, str]
    schema_head: str = Field(min_length=1)
    projector_version: str = Field(min_length=1)

    @property
    def platform(self) -> str:
        return self.image.platform


class LaunchReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[2]
    launch_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    hostname: str
    container_id: _SHA
    manifest_digest: _SHA
    image: PackagedImageIdentity
    actual_image_id: _IMAGE
    platform: Literal["linux/arm64", "linux/amd64"]


def canonical_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def identity_json(raw: bytes) -> Any:
    """JSON evidence must not contain multiple meanings for the same key."""
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_identity_key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def measure_inventory(root: Path, *, require_protected: bool = False) -> dict[str, str]:
    """Hash the complete fixed inventory, including extra and bytecode files."""
    return _measure(root, roots=INVENTORY_ROOTS, files=INVENTORY_FILES, require_protected=require_protected)


def measure_python_inventory(prefix: Path, *, require_protected: bool = False) -> dict[str, str]:
    """Official Python 3.13 image layout; producer and runtime use this exact function."""
    return _measure(prefix, roots=("lib/python3.13",),
        files=("bin/python3.13", "lib/libpython3.13.so.1.0"), require_protected=require_protected)


def _measure(root: Path, *, roots: tuple[str, ...], files: tuple[str, ...], require_protected: bool) -> dict[str, str]:
    paths = [root / name for name in files]
    if require_protected:
        assert_protected_file(root)
    for name in roots:
        directory = root / name
        if not directory.is_dir() or directory.is_symlink():
            raise ReleaseIdentityError("release_inventory_directory_missing")
        if require_protected:
            assert_protected_file(directory)
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ReleaseIdentityError("release_inventory_symlink")
            if require_protected:
                assert_protected_file(path)
            if path.is_file():
                paths.append(path)
    result = {}
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ReleaseIdentityError("release_inventory_file_missing")
        if require_protected:
            assert_protected_file(path)
        with path.open("rb") as handle:
            result[path.relative_to(root).as_posix()] = hashlib.file_digest(handle, "sha256").hexdigest()
    return result


def runtime_environment(environ: Mapping[str, str]) -> dict[str, str]:
    unknown = {key for key in environ if key.startswith("BFX_")} - (
        NONSECRET_ENV_KEYS | _SECRET_ENV | _PATH_ENV)
    if unknown:
        raise ReleaseIdentityError("release_unclassified_environment:" + ",".join(sorted(unknown)))
    return {key: value for key, value in environ.items() if key in NONSECRET_ENV_KEYS}


def assert_protected_file(path: Path) -> None:
    """Only root-owned, non-writable files on a read-only filesystem are trusted."""
    stat = path.stat()
    if path.is_symlink() or stat.st_uid != 0 or stat.st_mode & 0o022:
        raise ReleaseIdentityError("release_receipt_unprotected")
    if not os.statvfs(path).f_flag & os.ST_RDONLY:
        raise ReleaseIdentityError("release_receipt_mount_writable")


def _platform() -> str:
    architecture = {"aarch64": "arm64", "x86_64": "amd64"}.get(platform_module.machine())
    return f"{platform_module.system().lower()}/{architecture}"


@dataclass(frozen=True)
class VerifiedRelease:
    manifest: ReleaseManifest
    release_digest: str
    config_digest: str
    launch_id: str
    actual_image_id: str


@dataclass
class ReleaseRuntime:
    root: Path
    manifest_path: Path
    receipt_path: Path
    python_prefix: Path = Path("/usr/local")
    hostname: Callable[[], str] = socket.gethostname
    platform: Callable[[], str] = _platform
    environment: Callable[[], Mapping[str, str]] = lambda: runtime_environment(os.environ)

    @classmethod
    def from_environment(cls) -> ReleaseRuntime:
        root = Path(__file__).resolve().parents[3]
        if (os.environ.get("PYTHONDONTWRITEBYTECODE") != "1"
            or (root / ".env").exists()
            or any(os.environ.get(key) for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "LD_PRELOAD", "LD_LIBRARY_PATH"))
            or Path(sys.base_prefix) != Path("/usr/local")
            or Path(sys.executable).resolve() != Path("/usr/local/bin/python3.13")
            or Path(sys.prefix) != root / ".venv"
            or Path.cwd() != root
            or Path(sys.argv[0]).resolve() != root / "src/bfx_funding_bot/modules/marketfeed/daemon.py"
            or os.getuid() == 0):
            raise ReleaseIdentityError("release_python_environment_invalid")
        try:
            return cls(
                root=root,
                manifest_path=Path(os.environ["BFX_RELEASE_MANIFEST_PATH"]),
                receipt_path=Path(os.environ["BFX_RELEASE_LAUNCH_RECEIPT_PATH"]),
            )
        except KeyError as exc:
            raise ReleaseIdentityError("release_paths_missing") from exc

    def verify(self) -> VerifiedRelease:
        try:
            assert_protected_file(self.manifest_path)
            assert_protected_file(self.receipt_path)
            manifest = ReleaseManifest.model_validate(identity_json(self.manifest_path.read_bytes()))
            receipt = LaunchReceipt.model_validate(identity_json(self.receipt_path.read_bytes()))
            digest = canonical_digest(manifest.model_dump(mode="json"))
            if (
                receipt.manifest_digest != digest
                or receipt.image != manifest.image
                or receipt.actual_image_id not in (manifest.image.config_digest, manifest.image.manifest_digest)
                or receipt.platform != manifest.platform
                or self.platform() != manifest.platform
                or receipt.hostname != "bfx-" + receipt.launch_id
                or self.hostname() != receipt.hostname
            ):
                raise ReleaseIdentityError("release_launch_mismatch")
            if runtime_environment(manifest.environment) != manifest.environment:
                raise ReleaseIdentityError("release_manifest_secret_or_path_input")
            if dict(self.environment()) != manifest.environment:
                raise ReleaseIdentityError("release_environment_mismatch")
            for key in ("BFX_CELLS_YAML", "BFX_SAFETY_CONFIG", "BFX_FILL_MODEL_ARTIFACT"):
                if key in manifest.environment:
                    path = Path(manifest.environment[key])
                    if not path.is_absolute():
                        path = self.root / path
                    if not path.resolve().is_relative_to((self.root / "configs").resolve()) or not path.is_file():
                        raise ReleaseIdentityError("release_config_outside_inventory")
            inventory = measure_inventory(self.root, require_protected=True)
            if (inventory != manifest.inventory
                or measure_python_inventory(self.python_prefix, require_protected=True) != manifest.python_inventory):
                raise ReleaseIdentityError("release_inventory_mismatch")
            return VerifiedRelease(
                manifest=manifest, release_digest=digest,
                config_digest=canonical_digest({
                    "funding_rule_digest": RULE.digest,
                    "environment": manifest.environment,
                    "files": {key: value for key, value in inventory.items()
                              if key.startswith("configs/")},
                }),
                launch_id=receipt.launch_id,
                actual_image_id=receipt.actual_image_id,
            )
        except ReleaseIdentityError:
            raise
        except (OSError, ValueError) as exc:
            raise ReleaseIdentityError("release_identity_unavailable") from exc
