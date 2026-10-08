"""One definition of each operator-request outbox's actions, states and column split.

The migrations may not import application code, so they spell the same facts
out again; these tests keep every copy pinned to the models and to the shared
states in ``operator_requests``.
"""
import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.capital_tables import (
    POLICY_REQUEST_ACTIONS,
    CapitalPolicyRequestRow,
)
from bfx_funding_bot.modules.execution.operator_requests import REQUEST_STATES
from bfx_funding_bot.modules.execution.safety.tables import TradingControlRequestRow
from bfx_funding_bot.modules.execution.uncertainty_requests import (
    ResolutionIntent,
    ResolutionScope,
    request_values,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    UNCERTAINTY_RESOLUTION_ACTIONS,
    UncertaintyResolutionRequestRow,
)
from bfx_funding_bot.modules.ledger import RequestColumns

_VERSIONS = Path(__file__).resolve().parents[3] / "alembic/versions"
_MIGRATION = "1c435a35dcb4_trading_governance.py"
_UNCERTAINTY_MIGRATION = "f5a6b7c8d9e0_drop_pre_switch_request_evidence.py"
# The migration that last (re)created each outbox, and its constants' prefix.
_OUTBOXES = [
    (UncertaintyResolutionRequestRow, _UNCERTAINTY_MIGRATION, "UNCERTAINTY_"),
    (TradingControlRequestRow, "5b1e7c9d2a40_two_state_trading_control.py", ""),
    (CapitalPolicyRequestRow, "7d2a9c4e6b13_capital_policy_requests.py", ""),
]


def _migration(name: str = _MIGRATION):
    spec = importlib.util.spec_from_file_location("outbox_migration", _VERSIONS / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The migration that closed a product column (revoked its UPDATE, unmapped it).
_CAUSATION = "5e820d6dc7da_operator_request_causation.py"


@pytest.mark.parametrize(("model", "migration_file", "prefix"), _OUTBOXES)
def test_migration_grants_exactly_the_declared_column_split(model, migration_file, prefix) -> None:
    migration = _migration(migration_file)
    closed = _migration(_CAUSATION).CLOSED_COLUMNS.get(model.__tablename__)
    granted = tuple(getattr(migration, f"{prefix}WORKER_COLUMNS").split(","))
    assert tuple(getattr(migration, f"{prefix}REQUEST_COLUMNS").split(",")) == model.REQUEST_COLUMNS
    assert tuple(c for c in granted if c != closed) == model.WORKER_COLUMNS
    assert getattr(model, "CLOSED_COLUMNS", ()) == (() if closed is None else (closed,))


@pytest.mark.parametrize(("model", "migration_file", "prefix"), _OUTBOXES)
def test_every_column_belongs_to_exactly_one_writer(model, migration_file, prefix) -> None:
    columns = {column.name for column in model.__table__.columns}
    closed = set(getattr(model, "CLOSED_COLUMNS", ()))
    assert set(model.REQUEST_COLUMNS).isdisjoint(model.WORKER_COLUMNS)
    assert columns == set(model.REQUEST_COLUMNS) | set(model.WORKER_COLUMNS) | closed
    # The shared worker records these on every outcome.
    assert {"state", "processed_at_ms", "outcome_reason"} <= set(model.WORKER_COLUMNS)


@pytest.mark.parametrize("model", [model for model, _, _ in _OUTBOXES])
def test_the_causation_migration_guards_the_declared_request_columns(model) -> None:
    """Its grant assertion names exactly the columns no runtime role may UPDATE."""
    assert _migration(_CAUSATION).REQUEST_COLUMNS[model.__tablename__] == model.REQUEST_COLUMNS


@pytest.mark.parametrize("model", [TradingControlRequestRow, CapitalPolicyRequestRow])
def test_closed_product_columns_are_in_the_table_but_never_read_or_written(model) -> None:
    """The next release drops them while this one runs: nothing this image sends may name them."""
    from sqlalchemy import insert, inspect, select

    closed = set(model.CLOSED_COLUMNS)
    assert closed <= {column.name for column in model.__table__.columns}
    assert closed.isdisjoint(column.key for column in inspect(model).columns)
    assert all(not hasattr(model, column) for column in closed)
    for statement in (select(model), insert(model).values(**dict.fromkeys(model.REQUEST_COLUMNS))):
        assert closed.isdisjoint(str(statement).replace(",", " ").replace(".", " ").split())


def test_web_api_insert_names_only_the_granted_request_columns() -> None:
    values = request_values(
        ResolutionScope(uuid4(), "ci"),
        ResolutionIntent(
            uncertainty_id=uuid4(), action="mark_not_accepted", evidence_ref="obs:x",
            operator_id="operator-1",
        ),
        RequestColumns(observation_id=uuid4()),
        now_ms=1,
    )
    assert tuple(values) == UncertaintyResolutionRequestRow.REQUEST_COLUMNS


def test_migration_checks_match_the_declared_actions_and_states() -> None:
    migration = _migration()
    assert migration.UNCERTAINTY_ACTIONS == UNCERTAINTY_RESOLUTION_ACTIONS
    assert migration.REQUEST_STATES == REQUEST_STATES


def test_currency_toggle_actions_match_the_migration_and_the_web_api() -> None:
    from typing import get_args

    from bfx_funding_bot.modules.api.trading_control import CurrencyAction

    migration = _migration("7d2a9c4e6b13_capital_policy_requests.py")
    assert migration.ACTIONS == POLICY_REQUEST_ACTIONS == get_args(CurrencyAction)


def test_the_contract_drops_exactly_the_columns_its_first_step_closed() -> None:
    model = UncertaintyResolutionRequestRow
    closed = _migration("e4f5a6b7c8d9_close_pre_switch_request_evidence.py").CLOSED_COLUMNS
    dropped = _migration(_UNCERTAINTY_MIGRATION).DROPPED_COLUMNS
    assert dropped == closed
    assert set(dropped).isdisjoint(column.name for column in model.__table__.columns)
