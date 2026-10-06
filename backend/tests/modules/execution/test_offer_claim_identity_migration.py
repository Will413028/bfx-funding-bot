from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[3]
        / "alembic"
        / "versions"
        / "a6c9e2f4b7d1_add_offer_claim_identity_uniques.py"
    )
    spec = importlib.util.spec_from_file_location("offer_claim_identity_uniques", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration_connection() -> Iterator[Connection]:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table(
        "offer_claims",
        metadata,
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("cid", sa.BigInteger(), nullable=False),
        sa.Column("venue_offer_id", sa.Text()),
        sa.Column("execution_decision_id", sa.Text()),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        yield connection
    engine.dispose()


def test_upgrade_fails_before_ddl_with_deterministic_actionable_collision_diagnostic(
    migration_connection: Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    claims = sa.table(
        "offer_claims",
        sa.column("account_id"),
        sa.column("deployment_environment"),
        sa.column("cid"),
        sa.column("venue_offer_id"),
        sa.column("execution_decision_id"),
    )
    migration_connection.execute(
        sa.insert(claims),
        [
            {
                "account_id": "acct-a",
                "deployment_environment": "prod",
                "cid": 20,
                "venue_offer_id": "venue-dup",
                "execution_decision_id": "decision-20",
            },
            {
                "account_id": "acct-a",
                "deployment_environment": "prod",
                "cid": 10,
                "venue_offer_id": "venue-dup",
                "execution_decision_id": "decision-10",
            },
            {
                "account_id": "acct-b",
                "deployment_environment": "shadow",
                "cid": 31,
                "venue_offer_id": "venue-31",
                "execution_decision_id": "decision-dup",
            },
            {
                "account_id": "acct-b",
                "deployment_environment": "shadow",
                "cid": 30,
                "venue_offer_id": "venue-30",
                "execution_decision_id": "decision-dup",
            },
        ],
    )
    create_calls: list[str] = []
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(migration.op, "get_bind", lambda: migration_connection)
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda name, *args, **kwargs: create_calls.append(name),
    )

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    message = str(raised.value)
    assert create_calls == []
    assert "migration a6c9e2f4b7d1 blocked by 2 offer_claims identity collision(s)" in message
    venue_group = message.index("venue_offer_id='venue-dup'")
    decision_group = message.index("execution_decision_id='decision-dup'")
    assert venue_group < decision_group
    assert message.index("cid=10", venue_group) < message.index("cid=20", venue_group)
    assert "cid=10 venue_offer_id='venue-dup' execution_decision_id='decision-10'" in message
    assert "cid=30 venue_offer_id='venue-30' execution_decision_id='decision-dup'" in message
    assert "does not merge, delete, or quarantine rows" in message
    assert "uv run alembic upgrade head" in message


def test_clean_upgrade_creates_both_partial_unique_indexes_after_preflight(
    migration_connection: Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    create_calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(migration.op, "get_bind", lambda: migration_connection)
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda name, *args, **kwargs: create_calls.append((name, kwargs["unique"])),
    )

    migration.upgrade()

    assert create_calls == [
        ("uq_offer_claims_venue_offer_id", True),
        ("uq_offer_claims_execution_decision_id", True),
    ]


def test_online_postgres_upgrade_locks_claims_before_preflight_and_indexes(
    migration_connection: Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock closes the preflight-to-index-creation writer race."""
    migration = _load_migration()
    operations: list[str] = []

    class _PostgresConnection:
        dialect = SimpleNamespace(name="postgresql")

        def execute(self, statement: object) -> object:
            rendered = str(statement)
            if rendered.strip().startswith("LOCK TABLE"):
                operations.append("lock")
                return None
            operations.append("preflight")
            return migration_connection.execute(statement)  # type: ignore[arg-type]

    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(migration.op, "get_bind", lambda: _PostgresConnection())
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda name, *args, **kwargs: operations.append(name),
    )

    migration.upgrade()

    assert operations == [
        "lock",
        "preflight",
        "uq_offer_claims_venue_offer_id",
        "uq_offer_claims_execution_decision_id",
    ]


def test_offline_upgrade_emits_lock_then_preflight_before_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generated PostgreSQL migration keeps its collision check race-free."""
    migration = _load_migration()
    operations: list[str] = []
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: True)
    monkeypatch.setattr(
        migration.op,
        "execute",
        lambda statement: operations.append(str(statement).strip().split(maxsplit=1)[0]),
    )
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda name, *args, **kwargs: operations.append(name),
    )

    migration.upgrade()

    assert operations == [
        "LOCK",
        "DO",
        "uq_offer_claims_venue_offer_id",
        "uq_offer_claims_execution_decision_id",
    ]


def test_downgrade_drops_identity_indexes_in_reverse_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    drop_calls: list[str] = []
    monkeypatch.setattr(
        migration.op,
        "drop_index",
        lambda name, *args, **kwargs: drop_calls.append(name),
    )

    migration.downgrade()

    assert drop_calls == [
        "uq_offer_claims_execution_decision_id",
        "uq_offer_claims_venue_offer_id",
    ]
