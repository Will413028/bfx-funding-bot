"""Revision-chain guard for SP3 user_configs migration.

Uses importlib.util.spec_from_file_location because alembic's project-local
versions/ directory is not installed as a Python package and therefore
'alembic.versions.*' is not importable via importlib.import_module.
"""
import importlib.util
import pathlib


def _load_migration(revision_filename: str):
    p = pathlib.Path(__file__).parent.parent / "alembic" / "versions" / revision_filename
    spec = importlib.util.spec_from_file_location(revision_filename.removesuffix(".py"), p)
    assert spec is not None and spec.loader is not None, f"Cannot locate migration: {p}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_user_configs_migration_chains_off_sp2_head():
    mod = _load_migration("a2b3c4d5e6f7_add_user_configs.py")
    assert mod.revision == "a2b3c4d5e6f7"
    assert mod.down_revision == "f1a2b3c4d5e6"  # SP2 api_key_vault (prior head)
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)
