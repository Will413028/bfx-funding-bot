"""Every closed value set of an /api/v1 response is bound to the value set it reports (9k).

A response field typed ``Literal`` refuses any other value (500, or 503 where a read is
``_rendered``), so a value its source gains without the API following would break the
console. Most fields use the domain's type directly; the ones that cannot (a narrowing of a
domain type, a constant, a CHECK constraint) are listed here with their source, and every
``Literal`` field of every response model has to be listed: a new one without a source, or a
source that changes alone, fails. ``tests/integration/test_api_literal_checks.py`` compares
the CHECK-sourced ones with the migrated database.
"""
from __future__ import annotations

import importlib
import pkgutil
import re
from collections.abc import Callable
from types import UnionType
from typing import Literal, TypeAliasType, Union, get_args, get_origin, get_type_hints

from sqlalchemy import CheckConstraint, Table

import bfx_funding_bot.modules.api as api_package
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    AccountLifecycleStatus,
    AccountRole,
    CredentialLifecycleStatus,
)
from bfx_funding_bot.modules.api.schemas import ResponseModel
from bfx_funding_bot.modules.execution.operator_commands import CurrencyAction, TradingAction
from bfx_funding_bot.modules.execution.operator_requests import RequestState
from bfx_funding_bot.modules.execution.safety import trading_state
from bfx_funding_bot.modules.execution.safety.tables import FundingCancelAllAuditRow
from bfx_funding_bot.modules.execution.uncertainty_tables import ResolutionAction
from bfx_funding_bot.modules.ledger import OfferState, UncertaintyKind, UncertaintyState
from bfx_funding_bot.modules.ledger._internal.reads import UNCERTAINTY_KINDS
from bfx_funding_bot.modules.trading.policy import CapitalPolicy


def values(tp: object) -> frozenset[object]:
    return frozenset(get_args(tp.__value__ if isinstance(tp, TypeAliasType) else tp))


def check_values(table: object, name: str) -> frozenset[str]:
    """The quoted values of a model's ``<column> IN (...)`` CHECK constraint."""
    assert isinstance(table, Table)
    (check,) = (c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == name)
    return frozenset(re.findall(r"'([^']*)'", str(check.sqltext)))


# (model, field) -> the values it must allow, computed from its source.
SOURCES: dict[tuple[str, str], Callable[[], frozenset[object]]] = {
    # trading_control: constants and a CHECK constraint.
    ("TradingStateView", "state"): lambda: frozenset(trading_state.STATES),
    ("TradingStateView", "cause"): lambda: frozenset(trading_state.CAUSES),
    ("CancelAllPhase", "phase"): lambda: check_values(
        FundingCancelAllAuditRow.__table__, "ck_funding_cancel_all_audit_phase"),
    ("TradingControlRequest", "action"): lambda: values(TradingAction),
    ("TradingControlRequest", "state"): lambda: values(RequestState),
    ("CurrencyRequest", "action"): lambda: values(CurrencyAction),
    ("CurrencyRequest", "state"): lambda: values(RequestState),
    # The one value a queued request reports; the intersection is empty if the type drops it.
    ("KillRequested", "action"): lambda: frozenset({"kill"}) & values(TradingAction),
    ("KillRequested", "state"): lambda: frozenset({"requested"}) & values(RequestState),
    ("CurrencyDisableRequested", "action"): lambda: frozenset({"disable"}) & values(CurrencyAction),
    ("CurrencyDisableRequested", "state"): lambda: frozenset({"requested"}) & values(RequestState),
    # schemas: domain types, and two narrowings of them.
    ("ExchangeAccountResponse", "lifecycle_status"): lambda: (
        values(AccountLifecycleStatus) - {"retired"}),  # the list query excludes retired
    ("ExchangeAccountResponse", "role"): lambda: values(AccountRole),
    ("AccountCredentialResponse", "status"): lambda: (  # api_keys._credential_status
        values(CredentialLifecycleStatus) - {"active"} | {"verified", "unverified", "failed"}),
    ("OfferClaimResponse", "state"): lambda: values(OfferState),
    # uncertainties: the ledger's own types.
    ("UncertaintyResponse", "kind"): lambda: values(UncertaintyKind),
    ("UncertaintyResponse", "state"): lambda: values(UncertaintyState),
    ("ResolutionRequestResponse", "action"): lambda: values(ResolutionAction),
    ("ResolutionRequestResponse", "state"): lambda: values(RequestState),
    # funding_status: the daemon's capital policy, and the union's discriminator.
    ("CapitalPolicyStatus", "allocation_mode"): lambda: values(
        get_type_hints(CapitalPolicy)["allocation_mode"]),
    ("CapitalUnavailable", "capital_available"): lambda: frozenset({False}),
    ("CapitalAvailable", "capital_available"): lambda: frozenset({True}),
}


def literal_values(annotation: object) -> frozenset[object] | None:
    """The values a field's ``Literal`` allows (through aliases and ``| None``), else None."""
    if isinstance(annotation, TypeAliasType):
        return literal_values(annotation.__value__)
    origin = get_origin(annotation)
    if origin is Literal:
        return frozenset(get_args(annotation))
    if origin in (Union, UnionType):
        found = [v for arg in get_args(annotation) if (v := literal_values(arg)) is not None]
        return frozenset().union(*found) if found else None
    return None


def response_literals() -> dict[tuple[str, str], frozenset[object]]:
    for module in pkgutil.iter_modules(api_package.__path__):
        importlib.import_module(f"{api_package.__name__}.{module.name}")
    found: dict[tuple[str, str], frozenset[object]] = {}
    pending: list[type[ResponseModel]] = [ResponseModel]
    while pending:
        model = pending.pop()
        pending.extend(model.__subclasses__())
        for name, field in model.model_fields.items():
            allowed = literal_values(field.annotation)
            if allowed is not None:
                found[(model.__name__, name)] = allowed
    return found


def test_every_response_literal_has_a_source() -> None:
    assert set(response_literals()) == set(SOURCES)


def test_every_response_literal_allows_exactly_its_source() -> None:
    found = response_literals()
    mismatched = {key: (found[key], source()) for key, source in SOURCES.items()
                  if key in found and found[key] != source()}
    assert not mismatched


def test_the_api_allows_exactly_the_kinds_the_ledger_reports() -> None:
    """One kind the API does not allow would turn the whole uncertainty list into a 503;
    mypy holds ``UNCERTAINTY_KINDS`` to ``UncertaintyKind``, this holds the type to the map."""
    assert set(UNCERTAINTY_KINDS.values()) == values(UncertaintyKind)
    assert response_literals()[("UncertaintyResponse", "kind")] == values(UncertaintyKind)
