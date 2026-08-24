import importlib.util
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from bfx_funding_bot.modules.lending.tracking.artifact import FillModelArtifact
from bfx_funding_bot.modules.lending.tracking.tables import FillRateModelArtifactRow


def _artifact(*, metadata: Mapping[str, object]) -> FillModelArtifact:
    return FillModelArtifact(
        symbol="fUSD",
        period_agg="p2",
        horizon_h=4,
        source="candle",
        model_version="g13-candle-v1",
        schema_version=1,
        artifact_hash="artifact-v1",
        training_start_ms=1_000,
        training_end_ms=2_000,
        cutoff_ms=2_000,
        sample_count=180,
        confidence_min_samples=30,
        metadata=metadata,
    )


def _load_artifact_migration():
    path = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "f5b8d0e2f3c4_add_fill_model_artifacts.py"
    )
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None, f"Cannot locate migration: {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_fill_model_artifact_metadata_is_defensively_deep_immutable() -> None:
    original = {"nested": {"values": [1]}}
    artifact = _artifact(metadata=original)

    original["nested"]["values"].append(2)  # type: ignore[index]
    nested = cast(Mapping[str, object], artifact.metadata["nested"])
    values = cast(tuple[object, ...], nested["values"])

    assert values == (1,)
    assert artifact.metadata_for_storage() == {"nested": {"values": [1]}}
    with pytest.raises(TypeError):
        artifact.metadata["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        nested["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        values[0] = 2  # type: ignore[index]


def test_artifact_metadata_server_default_matches_migration(monkeypatch) -> None:
    migration = _load_artifact_migration()
    captured: dict[str, object] = {}

    def capture_create_table(name: str, *columns, **kwargs) -> None:
        captured[name] = {column.name: column for column in columns}

    monkeypatch.setattr(migration.op, "create_table", capture_create_table)
    monkeypatch.setattr(migration.op, "add_column", lambda *args, **kwargs: None)
    monkeypatch.setattr(migration.op, "create_foreign_key", lambda *args, **kwargs: None)
    migration.upgrade()

    migration_columns = cast(dict[str, object], captured["fill_rate_model_artifacts"])
    migration_column = migration_columns["metadata_json"]
    orm_column = FillRateModelArtifactRow.__table__.c.metadata_json

    assert migration_column.server_default is not None
    assert orm_column.server_default is not None
    assert str(migration_column.server_default.arg) == str(orm_column.server_default.arg)
