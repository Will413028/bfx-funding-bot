"""The legacy freeze (``e8f9a0b1c2d3``) and the archive (``c2d3e4f5a6b7``) name the same twelve
tables. Both migrations are applied and fixed; the live catalog is compared with them in
``tests/integration/test_legacy_archive_schema.py``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[2] / "alembic/versions"


def _load(filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), _VERSIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_archive_holds_exactly_the_frozen_tables() -> None:
    freeze = _load("e8f9a0b1c2d3_legacy_authority_freeze.py")
    archive = _load("c2d3e4f5a6b7_legacy_archive_schema.py")
    assert set(archive.TABLES) == set(freeze.FROZEN)
    assert not set(freeze.FROZEN) & set(freeze.SHARED)
