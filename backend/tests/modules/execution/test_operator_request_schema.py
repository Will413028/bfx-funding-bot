"""One definition of each operator-request outbox's actions, states and column split.

The migrations may not import application code, so they spell the same facts
out again; these tests keep every copy pinned to the models and to the shared
states in ``operator_requests``.
"""
import importlib.util
import re
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
_OUTBOXES = [
    (UncertaintyResolutionRequestRow, "b8e2d4f6a013_add_uncertainty_resolution_requests.py"),
    (TradingControlRequestRow, "0218f9ab59a2_add_trading_control.py"),
]


def _migration(filename: str):
    spec = importlib.util.spec_from_file_location("outbox_migration", _VERSIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _in_list(check: str, column: str) -> tuple[str, ...]:
    match = re.search(rf"{column} IN \(([^)]*)\)", check)
    assert match is not None, check
    return tuple(item.strip().strip("'") for item in match.group(1).split(","))


@pytest.mark.parametrize(("model", "filename"), _OUTBOXES)
def test_migration_grants_exactly_the_declared_column_split(model, filename) -> None:
    migration = _migration(filename)
    assert tuple(migration._REQUEST_COLUMNS.split(",")) == model.REQUEST_COLUMNS
    assert tuple(migration._WORKER_COLUMNS.split(",")) == model.WORKER_COLUMNS


@pytest.mark.parametrize(("model", "filename"), _OUTBOXES)
def test_every_column_belongs_to_exactly_one_writer(model, filename) -> None:
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
    source = (_VERSIONS / _OUTBOXES[0][1]).read_text()
    action_check = re.search(r'"(action IN \([^)]*\))"', source)
    state_check = re.search(r'"(state IN \([^)]*\))"', source)
    assert action_check is not None and state_check is not None
    assert _in_list(action_check.group(1), "action") == UNCERTAINTY_RESOLUTION_ACTIONS
    assert _in_list(state_check.group(1), "state") == REQUEST_STATES
