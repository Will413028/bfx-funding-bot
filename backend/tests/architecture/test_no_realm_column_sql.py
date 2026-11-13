"""No row, key, API field or domain type carries the deployment environment
(plan 2026-11-08, D3 and D7).

The live tables lost the column in ``545b2877e9bd``; a statement that names it fails at run
time, on a path a unit test may not reach. What is left of the name is the bot's check of
its setting against the database's stamp, the telemetry labels, and the frozen archive's
column. Any other file that names it is listed here by a failing test, not found in
production.

Mutation checks: add the column back to a store's INSERT; add a ``deployment_environment``
field to ``Scope``; add an ``environment`` field to ``CapitalScope``.
"""
import dataclasses
import inspect
from pathlib import Path

from bfx_funding_bot.core.sealed_credential import SealContext
from bfx_funding_bot.modules.execution.audit.model import AuditContext
from bfx_funding_bot.modules.execution.operator_commands import OperatorContext
from bfx_funding_bot.modules.execution.protocols import AccountContext
from bfx_funding_bot.modules.execution.uncertainty_requests import ResolutionScope
from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.simulated_venue.contracts import SimAccount
from bfx_funding_bot.modules.trading import capital

REPO = Path(__file__).resolve().parents[3]
ROOTS = ("backend/src", "backend/scripts", "deploy")
SKIPPED_DIRS = {"__pycache__", ".venv", "node_modules"}
NAME = "deployment_environment"

ALLOWED = {
    # The bot: reads the setting, compares it with the stamp, labels its telemetry.
    "backend/src/bfx_funding_bot/apps/bot.py",
    "backend/src/bfx_funding_bot/apps/config.py",
    "backend/src/bfx_funding_bot/apps/runtime/config.py",
    "backend/src/bfx_funding_bot/core/settings.py",
    # The web API: telemetry label only.
    "backend/src/bfx_funding_bot/apps/webapi.py",
    # Telemetry labels, injected by the assembly points.
    "backend/src/bfx_funding_bot/modules/execution/trading_status.py",
    "backend/src/bfx_funding_bot/modules/observability/metrics.py",
    "backend/src/bfx_funding_bot/modules/observability/resource.py",
    "backend/src/bfx_funding_bot/modules/observability/tracing.py",
    "backend/scripts/send_test_alert.py",
    "backend/scripts/send_test_error.py",
    # The frozen archive keeps its column; the reader declares it and does not filter on it.
    "backend/src/bfx_funding_bot/modules/execution/archived_execution_history.py",
    # Comments on receipts and boot reports written before the column was dropped.
    "deploy/vm/pgbackrest/ledger_digest.py",
}

SCOPE_TYPES = (Scope, ResolutionScope, OperatorContext, AccountContext, AuditContext,
               SealContext, SimAccount)
ENVIRONMENT_WORDS = {"env", "environment"}


def _files_naming_the_column() -> set[str]:
    found = set()
    for root in ROOTS:
        for path in (REPO / root).rglob("*"):
            if not path.is_file() or SKIPPED_DIRS & set(path.parts):
                continue
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            if NAME in text:
                found.add(str(path.relative_to(REPO)))
    return found


def test_only_the_stamp_check_telemetry_and_the_archive_name_the_column() -> None:
    assert _files_naming_the_column() == ALLOWED


def test_the_api_contract_has_no_environment_field() -> None:
    assert NAME not in (REPO / "backend" / "openapi.json").read_text()


def _capital_types() -> list[type]:
    return [member for _, member in inspect.getmembers(capital, inspect.isclass)
            if member.__module__ == capital.__name__ and dataclasses.is_dataclass(member)]


def test_scope_and_context_types_have_no_environment_field() -> None:
    capital_types = _capital_types()
    assert capital.CapitalScope in capital_types
    offenders = {}
    for scope_type in (*SCOPE_TYPES, *capital_types):
        assert dataclasses.is_dataclass(scope_type), scope_type
        names = [field.name for field in dataclasses.fields(scope_type)
                 if ENVIRONMENT_WORDS & set(field.name.split("_"))]
        if names:
            offenders[scope_type.__qualname__] = names
    assert offenders == {}
