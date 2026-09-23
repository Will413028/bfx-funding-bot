from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa

_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "8a1b2c3d4e5f_add_exchange_account_identity.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("identity_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.created_tables: dict[str, tuple[object, ...]] = {}
        self.added_columns: dict[str, list[sa.Column[object]]] = {}
        self.indexes: list[tuple[str, str, tuple[str, ...]]] = []

    def create_table(self, name: str, *elements: object, **_kwargs: object) -> None:
        self.created_tables[name] = elements

    def add_column(self, table_name: str, column: sa.Column[object], **_kwargs: object) -> None:
        self.added_columns.setdefault(table_name, []).append(column)

    def create_index(
        self, name: str, table_name: str, columns: list[str], **_kwargs: object
    ) -> None:
        self.indexes.append((name, table_name, tuple(columns)))

    def create_foreign_key(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("additive migration must define FKs inline")

    def drop_index(self, *_args: object, **_kwargs: object) -> None:
        return None

    def drop_column(self, *_args: object, **_kwargs: object) -> None:
        return None

    def drop_table(self, *_args: object, **_kwargs: object) -> None:
        return None


def _table_foreign_keys(elements: tuple[object, ...]) -> list[sa.ForeignKeyConstraint]:
    return [element for element in elements if isinstance(element, sa.ForeignKeyConstraint)]


def test_additive_revision_has_expected_chain_and_identity_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_migration()
    assert module.revision == "8a1b2c3d4e5f"
    assert module.down_revision == "f5b8d0e2f3c4"

    op = _RecordingOp()
    monkeypatch.setattr(module, "op", op)
    module.upgrade()

    assert {
        "exchange_accounts",
        "exchange_account_memberships",
        "exchange_account_credentials",
        "account_config_drafts",
        "legacy_account_realm_map",
    } <= op.created_tables.keys()

    for table_name in (
        "exchange_account_memberships",
        "exchange_account_credentials",
        "account_config_drafts",
        "legacy_account_realm_map",
    ):
        fks = _table_foreign_keys(op.created_tables[table_name])
        assert fks
        assert all(
            (fk.ondelete or "").upper() != "CASCADE"
            for fk in fks
        ), f"{table_name} must not cascade-delete money identity"

    account_id_columns = [
        column
        for elements in op.created_tables.values()
        for column in elements
        if isinstance(column, sa.Column) and column.name == "exchange_account_id"
    ]
    assert account_id_columns
    assert all(column.nullable is False for column in account_id_columns)
    assert all(isinstance(column.type, sa.dialects.postgresql.UUID) for column in account_id_columns)


def test_additive_revision_adds_nullable_uuid_to_every_money_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_migration()
    op = _RecordingOp()
    monkeypatch.setattr(module, "op", op)
    module.upgrade()

    expected_tables = {
        "event_log",
        "offer_claims",
        "position_state",
        "reconcile_observation",
        "execution_decisions",
        "diagnostics",
        "nav_peak",
        "trading_halt",
        "attribution_weekly",
        "config_regime",
        "api_keys",
        "user_configs",
    }
    assert expected_tables == op.added_columns.keys()
    for columns in op.added_columns.values():
        assert len(columns) == 1
        column = columns[0]
        assert column.name == "exchange_account_id"
        assert column.nullable is True
        assert isinstance(column.type, sa.dialects.postgresql.UUID)


def test_downgrade_refuses_when_identity_rows_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_migration()

    class _Result:
        def scalar_one(self) -> int:
            return 1

    class _Bind:
        def execute(self, _statement: sa.TextClause) -> _Result:
            return _Result()

    class _DowngradeOp(_RecordingOp):
        def get_bind(self) -> _Bind:
            return _Bind()

    op = _DowngradeOp()
    monkeypatch.setattr(module, "op", op)
    with pytest.raises(RuntimeError, match="dependent identity data"):
        module.downgrade()
