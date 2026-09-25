"""One definition of each operator-request outbox's actions, states and column split.

The migrations may not import application code, so they spell the same facts
out again; these tests keep every copy pinned to the models and to the shared
states in ``operator_requests``.
"""
import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.operator_requests import REQUEST_STATES
from bfx_funding_bot.modules.execution.safety.tables import TradingControlRequestRow
from bfx_funding_bot.modules.execution.uncertainty_resolution import (
    ResolutionIntent,
    ResolutionScope,
    request_values,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    UNCERTAINTY_RESOLUTION_ACTIONS,
    UncertaintyResolutionRequestRow,
)

_VERSIONS = Path(__file__).resolve().parents[3] / "alembic/versions"
_MIGRATION = "1c435a35dcb4_trading_governance.py"
# The migration that last (re)created each outbox, and its constants' prefix.
_OUTBOXES = [
    (UncertaintyResolutionRequestRow, _MIGRATION, "UNCERTAINTY_"),
    (TradingControlRequestRow, "5b1e7c9d2a40_two_state_trading_control.py", ""),
]


def _migration(name: str = _MIGRATION):
    spec = importlib.util.spec_from_file_location("outbox_migration", _VERSIONS / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("model", "migration_file", "prefix"), _OUTBOXES)
def test_migration_grants_exactly_the_declared_column_split(model, migration_file, prefix) -> None:
    migration = _migration(migration_file)
    assert tuple(getattr(migration, f"{prefix}REQUEST_COLUMNS").split(",")) == model.REQUEST_COLUMNS
    assert tuple(getattr(migration, f"{prefix}WORKER_COLUMNS").split(",")) == model.WORKER_COLUMNS


@pytest.mark.parametrize(("model", "migration_file", "prefix"), _OUTBOXES)
def test_every_column_belongs_to_exactly_one_writer(model, migration_file, prefix) -> None:
    columns = {column.name for column in model.__table__.columns}
    assert set(model.REQUEST_COLUMNS).isdisjoint(model.WORKER_COLUMNS)
    assert columns == set(model.REQUEST_COLUMNS) | set(model.WORKER_COLUMNS)
    # The shared worker records these on every outcome.
    assert {"state", "processed_at_ms", "outcome_reason"} <= set(model.WORKER_COLUMNS)


def test_web_api_insert_names_only_the_granted_request_columns() -> None:
    values = request_values(
        ResolutionScope(uuid4(), "ci"),
        ResolutionIntent(
            uncertainty_id=uuid4(), action="mark_not_accepted", reconcile_event_seq=1,
            operator_id="operator-1",
        ),
        now_ms=1,
    )
    assert tuple(values) == UncertaintyResolutionRequestRow.REQUEST_COLUMNS


def test_migration_checks_match_the_declared_actions_and_states() -> None:
    migration = _migration()
    assert migration.UNCERTAINTY_ACTIONS == UNCERTAINTY_RESOLUTION_ACTIONS
    assert migration.REQUEST_STATES == REQUEST_STATES
