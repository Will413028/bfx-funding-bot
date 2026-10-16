"""The operator commands service refuses on its own, without the web API's request models.

The web API validates a body before it reaches the service; the operator CLI does not
have that layer, so every bound the request models enforce is checked here again, with
the same constants. Each refusal below happens before the session is touched (``None``).
The HTTP codes and the order of the checks behind the web API are pinned by
``tests/modules/api/test_trading_control_router.py`` and ``test_uncertainties_router.py``.

Mutation checks (one at a time; revert after each):

* add a key to ``RESOLUTION_EVIDENCE_KEYS["bind_to_venue"]``: ``test_evidence_outside_*``
  here and in ``test_uncertainties_router.py``.
* drop ``decision`` from ``RESOLUTION_EVIDENCE_KEYS["manual_resolution"]``:
  ``test_each_adjudication_accepts_*`` here and in ``test_uncertainties_router.py``.
* ``resolve_context`` reads the environment before membership:
  ``test_membership_is_judged_before_the_deployment_environment``.
"""
from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.accounts.exchange_accounts import AccountRole
from bfx_funding_bot.modules.execution.operator_commands import (
    CONTROL_REASON_MAX,
    RESOLUTION_EVIDENCE_KEYS,
    RESOLUTION_REASON_MAX,
    OperatorCommandError,
    OperatorContext,
    ResolutionCommand,
    control_reason,
    manual_decision,
    request_currency,
    request_resolution,
    request_trading_control,
    resolution_evidence,
)
from bfx_funding_bot.modules.ledger import OperatorResolution, RequestColumns
from bfx_funding_bot.modules.ledger import ResolutionRejected as EvidenceRejected

ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
UNCERTAINTY = UUID("22222222-2222-2222-2222-222222222222")
REF = "ledger:v1:obs:33333333-3333-3333-3333-333333333333"
NO_SESSION = cast(AsyncSession, None)


def _context(role: str = "owner") -> OperatorContext:
    return OperatorContext(exchange_account_id=ACCOUNT, user_id="operator",
                           role=cast(AccountRole, role), lifecycle_status="active",
                           deployment_environment="ci")


class _Columns:
    """Only ``columns`` is reached before a refusal; a stale reference raises."""

    def columns(self, evidence_ref: str) -> RequestColumns:
        if evidence_ref != REF:
            raise EvidenceRejected("stale_reconcile_fence")
        return RequestColumns(observation_id=UUID(REF.rsplit(":", 1)[1]))


def _refusal(exc: pytest.ExceptionInfo[OperatorCommandError]) -> tuple[str, str]:
    return exc.value.kind, exc.value.code


async def _resolve(command: ResolutionCommand, *, role: str = "owner") -> tuple[str, str]:
    with pytest.raises(OperatorCommandError) as exc:
        await request_resolution(NO_SESSION, _context(role), cast(OperatorResolution, _Columns()),
                                 command, now_ms=1)
    return _refusal(exc)


@pytest.mark.parametrize("reason", ["", "  \t", "x" * (CONTROL_REASON_MAX + 1)])
def test_a_control_reason_is_required_not_blank_and_bounded(reason: str) -> None:
    with pytest.raises(OperatorCommandError) as exc:
        control_reason(reason)
    assert _refusal(exc) == ("invalid", "invalid_reason")
    assert control_reason("x" * CONTROL_REASON_MAX) == "x" * CONTROL_REASON_MAX


@pytest.mark.asyncio
async def test_a_viewer_is_refused_before_anything_is_validated() -> None:
    with pytest.raises(OperatorCommandError) as exc:
        await request_trading_control(NO_SESSION, _context("viewer"), action="resume",
                                      reason="", now_ms=1)
    assert _refusal(exc) == ("forbidden", "account_write_required")
    assert await _resolve(ResolutionCommand(UNCERTAINTY, "mark_not_accepted", "42"),
                          role="viewer") == ("forbidden", "account_write_required")


@pytest.mark.asyncio
@pytest.mark.parametrize(("symbol", "reason", "code"), [
    ("fust", "x", "invalid_symbol"),
    ("UST", "x", "invalid_symbol"),
    ("fUST", " ", "invalid_reason"),
])
async def test_a_currency_request_needs_a_funding_symbol_and_a_reason(symbol, reason, code) -> None:
    with pytest.raises(OperatorCommandError) as exc:
        await request_currency(NO_SESSION, _context(), symbol=symbol, action="disable",
                               reason=reason, now_ms=1)
    assert _refusal(exc) == ("invalid", code)


@pytest.mark.parametrize(("action", "evidence"), [
    (action, {key: 1}) for action, keys in RESOLUTION_EVIDENCE_KEYS.items() for key in keys
] + [("manual_resolution", {"decision": "not_accepted"}), ("bind_to_venue", {})])
def test_each_adjudication_accepts_its_evidence_keys(action, evidence: dict[str, Any]) -> None:
    assert resolution_evidence(action, evidence) == evidence


@pytest.mark.parametrize(("action", "evidence"), [
    ("bind_to_venue", {"decision": "not_accepted"}),
    ("bind_to_venue", {"venueOfferId": "9001"}),
    ("mark_not_accepted", {"decision": "not_accepted"}),
    ("manual_resolution", {"candidateCount": 1}),
    ("mark_not_accepted", {"candidateCount": "x" * 20_000}),
])
def test_evidence_outside_an_adjudications_allow_list_is_refused(action, evidence) -> None:
    with pytest.raises(OperatorCommandError) as exc:
        resolution_evidence(action, evidence)
    assert _refusal(exc) == ("invalid", "invalid_resolution_evidence")


@pytest.mark.parametrize(("evidence", "decision"), [
    ({"decision": "not_accepted"}, "not_accepted"),
    ({"decision": 7}, None),
    ({}, None),
])
def test_a_manual_decision_is_the_lone_decision_string(evidence, decision) -> None:
    assert manual_decision(evidence) == decision


@pytest.mark.asyncio
@pytest.mark.parametrize(("command", "code"), [
    (ResolutionCommand(UNCERTAINTY, "manual_resolution", REF), "invalid_reason"),
    (ResolutionCommand(UNCERTAINTY, "manual_resolution", REF, reason=""), "invalid_reason"),
    (ResolutionCommand(UNCERTAINTY, "mark_not_accepted", REF,
                       reason="x" * (RESOLUTION_REASON_MAX + 1)), "invalid_reason"),
    (ResolutionCommand(UNCERTAINTY, "bind_to_venue", REF), "invalid_venue_offer_id"),
    (ResolutionCommand(UNCERTAINTY, "bind_to_venue", REF, venue_offer_id="9" * 257),
     "invalid_venue_offer_id"),
    (ResolutionCommand(UNCERTAINTY, "mark_not_accepted", REF, operator_uuid=""),
     "operator_required"),
    (ResolutionCommand(UNCERTAINTY, "mark_not_accepted", REF, operator_uuid="   "),
     "operator_required"),
])
async def test_an_adjudication_out_of_bounds_is_invalid(command, code) -> None:
    assert await _resolve(command) == ("invalid", code)


@pytest.mark.asyncio
async def test_the_evidence_reference_is_judged_before_who_asked() -> None:
    assert await _resolve(ResolutionCommand(
        UNCERTAINTY, "mark_not_accepted", "42", operator_uuid="someone-else",
    )) == ("conflict", "stale_reconcile_fence")
    assert await _resolve(ResolutionCommand(
        UNCERTAINTY, "mark_not_accepted", REF, operator_uuid="someone-else",
    )) == ("forbidden", "operator_identity_mismatch")
