"""One definition of the adjudication outbox's actions, states and column split.

The migration may not import application code, so it spells the same facts out
again; these tests keep every copy pinned to the constants.
"""
import importlib.util
import re
from pathlib import Path
from uuid import uuid4

from bfx_funding_bot.modules.execution.uncertainty_resolution import (
    ResolutionIntent,
    ResolutionScope,
    request_values,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    UNCERTAINTY_REQUEST_COLUMNS,
    UNCERTAINTY_REQUEST_STATES,
    UNCERTAINTY_RESOLUTION_ACTIONS,
    UNCERTAINTY_WORKER_COLUMNS,
    UncertaintyResolutionRequestRow,
)

_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic/versions/b8e2d4f6a013_add_uncertainty_resolution_requests.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location("outbox_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _in_list(check: str, column: str) -> tuple[str, ...]:
    match = re.search(rf"{column} IN \(([^)]*)\)", check)
    assert match is not None, check
    return tuple(item.strip().strip("'") for item in match.group(1).split(","))


def test_migration_grants_exactly_the_declared_column_split() -> None:
    migration = _migration()
    assert tuple(migration._REQUEST_COLUMNS.split(",")) == UNCERTAINTY_REQUEST_COLUMNS
    assert tuple(migration._WORKER_COLUMNS.split(",")) == UNCERTAINTY_WORKER_COLUMNS


def test_every_column_belongs_to_exactly_one_writer() -> None:
    columns = tuple(column.name for column in UncertaintyResolutionRequestRow.__table__.columns)
    assert set(UNCERTAINTY_REQUEST_COLUMNS).isdisjoint(UNCERTAINTY_WORKER_COLUMNS)
    assert set(columns) == set(UNCERTAINTY_REQUEST_COLUMNS) | set(UNCERTAINTY_WORKER_COLUMNS)


def test_web_api_insert_names_only_the_granted_request_columns() -> None:
    values = request_values(
        ResolutionScope(uuid4(), "ci"),
        ResolutionIntent(
            uncertainty_id=uuid4(), action="mark_not_accepted", reconcile_event_seq=1,
            operator_id="operator-1",
        ),
        now_ms=1,
    )
    assert tuple(values) == UNCERTAINTY_REQUEST_COLUMNS


def test_migration_checks_match_the_declared_actions_and_states() -> None:
    source = _MIGRATION.read_text()
    action_check = re.search(r'"(action IN \([^)]*\))"', source)
    state_check = re.search(r'"(state IN \([^)]*\))"', source)
    assert action_check is not None and state_check is not None
    assert _in_list(action_check.group(1), "action") == UNCERTAINTY_RESOLUTION_ACTIONS
    assert _in_list(state_check.group(1), "state") == UNCERTAINTY_REQUEST_STATES
