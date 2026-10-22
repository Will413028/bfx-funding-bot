"""The /api/v1 wire format, byte for byte (9k).

Every JSON route of the web API is called through the real routers and FastAPI's own
response handling, and its body is compared with ``golden/responses.json``. The golden
file was captured from the routers *before* they declared response models, so this test
is the proof that declaring them changed no field name, casing, envelope, number type or
null. The data sources are the routers' own seams: SQLite rows where a router queries the
session itself, and the operator-command and read-model ports elsewhere (their own
behaviour is tested beside them).

Change the golden file only together with an intended API change (and the regenerated
``backend/openapi.json`` and frontend types; ``scripts/dump_openapi.py``).
"""
from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.exceptions import ResponseValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts import config_service, vault
from bfx_funding_bot.modules.accounts.exchange_accounts import grant_membership
from bfx_funding_bot.modules.accounts.tables import (
    AccountConfigDraft,
    ExchangeAccount,
    ExchangeAccountCredential,
)
from bfx_funding_bot.modules.api import public
from bfx_funding_bot.modules.api.account_scope import require_account_member
from bfx_funding_bot.modules.api.api_keys import build_api_keys_router
from bfx_funding_bot.modules.api.attribution import build_attribution_router
from bfx_funding_bot.modules.api.config import build_config_router
from bfx_funding_bot.modules.api.deps import ReadModels, get_session
from bfx_funding_bot.modules.api.funding_status import build_funding_status_router
from bfx_funding_bot.modules.api.projections import build_projections_router
from bfx_funding_bot.modules.api.public import build_public_router
from bfx_funding_bot.modules.api.routers import build_router
from bfx_funding_bot.modules.api.schemas import OfferClaimResponse, install_response_shape_handler
from bfx_funding_bot.modules.api.trading_control import build_trading_control_router
from bfx_funding_bot.modules.api.uncertainties import build_uncertainties_router
from bfx_funding_bot.modules.deployments.tables import DeploymentRow
from bfx_funding_bot.modules.execution import operator_commands
from bfx_funding_bot.modules.execution.capital_tables import CapitalPolicyRequestRow
from bfx_funding_bot.modules.execution.operator_commands import OperatorContext, UncertaintyDetail
from bfx_funding_bot.modules.execution.safety.tables import (
    FundingCancelAllAuditRow,
    TradingControlRequestRow,
    TradingStateRow,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import UncertaintyResolutionRequestRow
from bfx_funding_bot.modules.ledger import (
    ExecutionEventView,
    ExecutionPage,
    OfferView,
    PolicyRefused,
    PositionView,
    ResolutionEvidence,
    UncertaintyView,
)
from bfx_funding_bot.modules.ledger.tables import CapitalPolicyHeadRow
from bfx_funding_bot.modules.live_validation.tables import AttributionWeeklyRow
from bfx_funding_bot.modules.trading.capital import AppliedPolicy
from bfx_funding_bot.modules.trading.policy import CapitalPolicy, OfferEnvelope

GOLDEN = Path(__file__).parent / "golden" / "responses.json"

ACCOUNT = UUID("550e8400-e29b-41d4-a716-446655440000")
BASE = f"/api/v1/exchange-accounts/{ACCOUNT}"
CREDENTIAL = UUID("11111111-1111-4111-8111-111111111111")
DRAFT = UUID("22222222-2222-4222-8222-222222222222")
# A letter in each seeded id: SQLite stores an all-digit UUID text as a number.
KILL_REQUEST = UUID("33333333-3333-4333-8333-33333333333a")
CURRENCY_REQUEST = UUID("44444444-4444-4444-8444-44444444444a")
UNCERTAINTY = UUID("55555555-5555-4555-8555-555555555555")
RESOLUTION_REQUEST = UUID("66666666-6666-4666-8666-666666666666")
OBSERVATION = UUID("77777777-7777-4777-8777-777777777777")
CREATED = datetime(2026, 10, 1, 8, 30, tzinfo=UTC)
UPDATED = datetime(2026, 10, 2, 9, 45, tzinfo=UTC)

CONTEXT = OperatorContext(exchange_account_id=ACCOUNT, user_id="user_abc", role="owner",
                          lifecycle_status="active", deployment_environment="prod")


def _credential(**columns: Any) -> ExchangeAccountCredential:
    values: dict[str, Any] = {
        "id": CREDENTIAL, "exchange_account_id": ACCOUNT, "venue": "bitfinex", "label": "main",
        "api_key": "pub-key-1", "lifecycle_status": "active", "verified_at": UPDATED,
        "last_verify_error": None, "created_at": CREATED}
    return ExchangeAccountCredential(**(values | columns))


def _draft(config: dict[str, Any]) -> AccountConfigDraft:
    return AccountConfigDraft(id=DRAFT, exchange_account_id=ACCOUNT, config=config, revision=3,
                              source="operator", created_at=CREATED, updated_at=UPDATED)


STORED_CONFIG = {"currency": "fUST", "amount": {"min": 150.0, "max": 5000.0},
                 "rate": {"min": 0.0001, "max": 0.002}, "period": {"min": 2.0, "max": 30.0},
                 "autoRenew": True}
PUT_BODY = {"currency": "fUSD", "amount": {"min": 200, "max": 1000},
            "rate": {"min": 0.00015, "max": 0.001}, "period": {"min": 2, "max": 7},
            "autoRenew": False}


def _trading_request(state: str = "applied") -> TradingControlRequestRow:
    return TradingControlRequestRow(
        request_id=KILL_REQUEST, exchange_account_id=ACCOUNT, deployment_environment="prod",
        action="kill", reason="venue incident", requested_by="user_abc",
        created_at_ms=1_760_000_000_000, state=state,
        processed_at_ms=None if state == "requested" else 1_760_000_005_000,
        outcome_reason=None)


def _currency_request(state: str = "rejected") -> CapitalPolicyRequestRow:
    return CapitalPolicyRequestRow(
        request_id=CURRENCY_REQUEST, exchange_account_id=ACCOUNT, deployment_environment="prod",
        symbol="fUST", action="disable", reason="pause fUST",
        requested_by="user_abc", created_at_ms=1_760_000_010_000, state=state,
        processed_at_ms=1_760_000_012_000, outcome_reason="policy_unchanged")


def _overview(phase: str = "acknowledged") -> dict[str, Any]:
    """A ``trading_overview`` value, for a phase no database row can hold (the golden calls
    read the real one from the rows ``_seed`` writes)."""
    return {
        "trading_state": {"id": 9, "state": "HALTED", "cause": "operator", "actor": "user_abc",
                          "reason": "venue incident", "at_ms": 1_760_000_004_000},
        "cancel_all": [
            {"currency": "UST", "phase": phase, "detail": None, "at_ms": 1_760_000_004_500},
            {"currency": "USD", "phase": "requested", "detail": None,
             "at_ms": 1_760_000_004_400},
        ],
        "requests": [operator_commands.trading_request_view(_trading_request())],
        "currencies": [
            {"symbol": "fUSD", "revision": 2, "policy_error": "policy_unreadable",
             "enabled": None, "max_offer_amount": None, "envelope": None, "requests": []},
            {"symbol": "fUST", "revision": 5, "policy_error": None, "enabled": True,
             "max_offer_amount": "2500",
             "envelope": {"min_period_days": 2, "max_period_days": 30, "max_open_offers": 4,
                          "rate_floor_ratio": "0.9", "min_rate_apr": "4.5"},
             "requests": [operator_commands.currency_request_view(_currency_request())]},
        ],
    }


def _resolution_request(state: str = "rejected") -> UncertaintyResolutionRequestRow:
    return UncertaintyResolutionRequestRow(
        request_id=RESOLUTION_REQUEST, uncertainty_id=UNCERTAINTY, action="mark_not_accepted",
        observation_id=OBSERVATION, requested_by="user_abc", created_at_ms=1_760_000_020_000,
        state=state, processed_at_ms=1_760_000_021_000, outcome_reason="stale_reconcile_fence")


def _uncertainty(*, state: str = "open", latest: bool = True) -> UncertaintyDetail:
    return UncertaintyDetail(
        view=UncertaintyView(
            uncertainty_id=UNCERTAINTY, kind="submit_outcome_unknown", symbol="fUST",
            state=state, intended_amount=Decimal("150.50"),
            evidence={"reason": "timeout", "observed_at_ms": 1_760_000_015_000,
                      "candidate_count": 2, "coverage": {"offers": True, "credits": False},
                      "secret_header": "never shown"}),
        context=ResolutionEvidence(
            evidence_ref=f"obs:{OBSERVATION}", query_started_at_ms=1_760_000_016_000,
            query_finished_at_ms=1_760_000_017_000, candidate_venue_offer_ids=("v1", "v2"),
            candidate_count=2),
        latest_request=_resolution_request() if latest else None,
    )


def funding_snapshot() -> dict[str, Any]:
    """``/admin/trading-status`` as the daemon serves it, fields the console ignores included."""
    policy = {"enabled": True, "reserve_amount": "100", "allocation_mode": "all_available",
              "max_cell_fraction": "0.70", "max_offer_amount": None, "envelope": None}
    return {
        "phase": "live", "account_id": str(ACCOUNT), "deployment_environment": "prod",
        "configured_cells": [{"symbol": "fUST", "cell": "fUST_a30", "strategy": "mean_reversion",
                              "period": "a30"}],
        "process_started_at": "2026-10-01T08:00:00+00:00",
        "halt": {"halted": False, "reason": None, "guard_installed": True, "note": None,
                 "sources": {"persisted": None}, "history": []},
        "deployment": {"change_class": "code"},
        "guards": [{"name": "manual_kill"}],
        "symbols": {
            "fUSD": {"capital_available": False, "reason": "policy_disabled",
                     "policy_revision": 2, "policy_digest": "d2", "policy": policy},
            "fUST": {"capital_available": True, "policy_revision": 5, "policy_digest": "d5",
                     "basis_token": "b5", "policy": policy,
                     "available_balance": "9007199254740993.123456789",
                     "unreflected_commitments": "0.000000001", "total_capital": "1200.5",
                     "spendable": "1100.5", "unattributed_credit_exposure": "0",
                     "cells": {"fUST_a30": {"spendable": "1100.5", "cell_limit": "840.35",
                                            "cell_headroom": "840.35",
                                            "max_new_offer": "840.35", "reason": None}}},
        },
        "last_submit_attempt": None,
        "trading_readiness": {"trading_ready": True, "reason": None},
    }


def funding_dry_run() -> dict[str, Any]:
    """``/admin/dry-evaluate`` as the daemon serves it."""
    return {
        "account_id": str(ACCOUNT), "deployment_environment": "prod", "would_submit_any": False,
        "symbols": {"fUST": {
            "would_submit": False, "blocked_by": "manual_kill",
            "cells": {"fUST_a30": {"would_submit": False, "blocked_by": "manual_kill"}},
            "guards": [{"name": "manual_kill", "allowed": False, "reason": "halted",
                        "internal_error": False}],
            "decision": {"decision_outcome": "POST", "symbol": "fUST",
                         "offer_amount_usdt": "150", "offer_rate": "0.0002",
                         "offer_duration_days": 2}}},
    }


class _Reads:
    async def list_positions(self, session: Any, scope: Any) -> tuple[PositionView, ...]:
        return (
            PositionView("fUSD", Decimal("0"), Decimal("0"), Decimal("0"), None, None, 1000, None),
            PositionView("fUST", Decimal("11.50"), Decimal("22.25"), Decimal("2314.03"),
                         Decimal("4"), 3, 1000, 2000),
        )

    async def list_offers(self, session: Any, scope: Any, *, states: Any) -> tuple[OfferView, ...]:
        return (
            OfferView("o-2", None, "pending", "fUST", Decimal("100"), 1002, 2002),
            OfferView("o-1", "v1", "claimed", "fUST", Decimal("1000"), 1001, 2001),
        )


class _History:
    async def list_executions(self, session: Any, scope: Any, *, before: Any, limit: int,
                              event_type: Any) -> ExecutionPage:
        return ExecutionPage(events=(
            ExecutionEventView("j:3", "ORDER_FILL", 3000, "fUST", "v1", None, "100.5", 0.00017),
            ExecutionEventView("j:2", "SUBMIT_OUTCOME_UNKNOWN", 2000, "fUST", None, 42, None,
                               None),
        ), next_before="cursor-2")


class _Store:
    """The scope's applied capital policies (the ledger's PolicyStore, tested beside it)."""

    async def read_applied(self, session: Any, *, symbol: str) -> AppliedPolicy:
        if symbol != "fUST":
            raise PolicyRefused("policy_unreadable")
        return AppliedPolicy(
            account_id=ACCOUNT, environment="prod", symbol=symbol, revision=5, digest="d5",
            revision_id=UUID(int=5),
            policy=CapitalPolicy(enabled=True, max_offer_amount=Decimal("2500"),
                                 envelope=OfferEnvelope(min_period_days=2, max_period_days=30,
                                                        max_open_offers=4,
                                                        rate_floor_ratio=Decimal("0.9"),
                                                        min_rate_apr=Decimal("0.045"))))


class _Empty:
    """Every read model with nothing to report."""

    async def list_positions(self, session: Any, scope: Any) -> tuple[PositionView, ...]:
        return ()

    async def list_offers(self, session: Any, scope: Any, *, states: Any) -> tuple[OfferView, ...]:
        return ()

    async def list_executions(self, session: Any, scope: Any, *, before: Any, limit: int,
                              event_type: Any) -> ExecutionPage:
        return ExecutionPage(events=(), next_before=None)


def build_app(funding_reader: Any = None, *, empty: bool = False) -> FastAPI:
    """Every /api/v1 router, mounted as ``apps/webapi.py`` mounts them."""

    async def daemon(path: str) -> dict[str, Any]:
        return funding_snapshot() if path == "/admin/trading-status" else funding_dry_run()

    app = FastAPI()
    install_response_shape_handler(app)
    app.include_router(build_router())
    app.include_router(build_api_keys_router())
    app.include_router(build_attribution_router())
    app.include_router(build_config_router())
    app.include_router(build_funding_status_router(reader=funding_reader or daemon))
    app.include_router(build_projections_router())
    app.include_router(build_public_router())
    app.include_router(build_uncertainties_router())
    app.include_router(build_trading_control_router())
    unused: Any = object()
    reads: Any = _Empty() if empty else _Reads()
    history: Any = _Empty() if empty else _History()
    app.state.read_models = ReadModels(reads, unused, unused, history, lambda scope: _Store())
    return app


async def _seed(engine: AsyncEngine) -> async_sessionmaker[Any]:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(ExchangeAccount(id=ACCOUNT, venue="bitfinex", label="Primary",
                                    lifecycle_status="halted"))
        await session.flush()
        await grant_membership(session, exchange_account_id=ACCOUNT, user_id="user_abc",
                               role="owner")
        for cell, week, realized in (("fUST_a30", 1_782_691_200_000, Decimal("6.205")),
                                     ("fUST_a30", 1_783_296_000_000, None)):
            session.add(AttributionWeeklyRow(
                deployment_environment="prod", account_id="default", exchange_account_id=ACCOUNT,
                cell=cell, week_start_ms=week, week_end_ms=week + 604_800_000, n_fills=2,
                gross_interest_usdt=Decimal("0.2"), net_interest_usdt=Decimal("0.17"),
                capital_days=Decimal("1000"), realized_apr_net_pct=realized,
                baseline_close_apr_net_pct=Decimal("6.205"), baseline_frr_apr_net_pct=None,
                baseline_frr_util_apr_net_pct=Decimal("4.34"), computed_at=UPDATED))
        session.add(DeploymentRow(
            attempt_id=UUID(int=9), started_at=CREATED, finished_at=UPDATED,
            source_revision="a" * 40, backend_digest="sha256:" + "b" * 64,
            migrations_applied=False, outcome="deployed", detail="ok"))
        # What operator_commands.trading_overview reads: the HALTED in force and its
        # cancel-all, the kill request, and two currencies (one policy unreadable).
        session.add(_trading_request())
        session.add(_currency_request())
        session.add(TradingStateRow(
            id=9, exchange_account_id=ACCOUNT, deployment_environment="prod", state="HALTED",
            cause="operator", actor="user_abc", reason="venue incident",
            created_at_ms=1_760_000_004_000))
        for attempt, currency, phase, at_ms in ((1, "UST", "requested", 1_760_000_004_300),
                                                (2, "USD", "requested", 1_760_000_004_400),
                                                (1, "UST", "acknowledged", 1_760_000_004_500)):
            session.add(FundingCancelAllAuditRow(
                exchange_account_id=ACCOUNT, deployment_environment="prod", trading_state_id=9,
                attempt_id=UUID(int=0xA0 + attempt), currency=currency, phase=phase, actor="user_abc",
                occurred_at_ms=at_ms))
            await session.flush()  # the audit's id orders the phases
        for symbol, revision in (("fUSD", 2), ("fUST", 5)):
            session.add(CapitalPolicyHeadRow(
                exchange_account_id=ACCOUNT, deployment_environment="prod", symbol=symbol,
                revision_id=UUID(int=0xA0 + revision), revision=revision))
        await session.commit()
    return factory


@pytest.fixture
def patched_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    monkeypatch.setenv("BFX_PUBLIC_EXCHANGE_ACCOUNT_ID", str(ACCOUNT))
    monkeypatch.setenv("BFX_IMAGE_DIGEST", "sha256:" + "c" * 64)
    monkeypatch.setenv("BFX_SOURCE_REVISION", "d" * 40)

    async def list_credentials(session: Any, **_: Any) -> list[ExchangeAccountCredential]:
        return [_credential(), _credential(id=UUID(int=2), label="old", lifecycle_status="revoked",
                                           verified_at=None, last_verify_error="bad nonce")]

    async def get_draft(session: Any, **_: Any) -> AccountConfigDraft:
        return _draft(STORED_CONFIG)

    async def upsert_draft(session: Any, *, config: dict[str, Any], **_: Any) -> AccountConfigDraft:
        return _draft(config)

    async def queued(*args: Any, **kwargs: Any) -> UUID:
        return KILL_REQUEST if kwargs.get("action") == "kill" else CURRENCY_REQUEST

    async def trading_request(*args: Any) -> TradingControlRequestRow:
        return _trading_request()

    async def currency_request(*args: Any) -> CapitalPolicyRequestRow:
        return _currency_request()

    async def uncertainties(*args: Any, **kwargs: Any) -> list[UncertaintyDetail]:
        return [_uncertainty(), _uncertainty(state="resolved", latest=False)]

    async def uncertainty(*args: Any) -> UncertaintyDetail:
        return _uncertainty()

    async def resolution_request(*args: Any) -> UncertaintyResolutionRequestRow:
        return _resolution_request()

    monkeypatch.setattr(vault, "list_account_credentials", list_credentials)
    monkeypatch.setattr(config_service, "get_account_config_draft", get_draft)
    monkeypatch.setattr(config_service, "upsert_account_config_draft_for_user", upsert_draft)
    monkeypatch.setattr(operator_commands, "request_trading_control", queued)
    monkeypatch.setattr(operator_commands, "request_currency", queued)
    monkeypatch.setattr(operator_commands, "get_trading_request", trading_request)
    monkeypatch.setattr(operator_commands, "get_currency_request", currency_request)
    monkeypatch.setattr(operator_commands, "list_uncertainties", uncertainties)
    monkeypatch.setattr(operator_commands, "get_uncertainty", uncertainty)
    monkeypatch.setattr(operator_commands, "get_resolution_request", resolution_request)


def client_for(app: FastAPI, factory: async_sessionmaker[Any],
               principal: Principal | None = None) -> TestClient:
    async def session() -> Any:
        async with factory() as s:
            yield s
            await s.commit()

    app.dependency_overrides[require_operator] = lambda: principal or Principal(
        "user_abc", "will@example.com", "operator")
    app.dependency_overrides[require_account_member] = lambda: CONTEXT
    app.dependency_overrides[get_session] = session
    return TestClient(app, raise_server_exceptions=False)


@pytest_asyncio.fixture
async def factory(sqlite_engine: AsyncEngine) -> async_sessionmaker[Any]:
    return await _seed(sqlite_engine)


@pytest_asyncio.fixture
async def client(factory: async_sessionmaker[Any], patched_ports: None,
                 ) -> AsyncIterator[TestClient]:
    with client_for(build_app(), factory) as c:
        yield c


@pytest_asyncio.fixture
async def empty_client(sqlite_engine: AsyncEngine, patched_ports: None,
                       monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TestClient]:
    """Every list empty: an operator without an email or any account membership, an account
    with no rows, read models with nothing to report."""
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as session:
        session.add(ExchangeAccount(id=ACCOUNT, venue="bitfinex", label="Primary",
                                    lifecycle_status="active"))
        await session.commit()

    async def nothing(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(vault, "list_account_credentials", nothing)
    monkeypatch.setattr(operator_commands, "list_uncertainties", nothing)
    # The proof page stamps an empty series with the time of the request.
    monkeypatch.setattr(public, "datetime", _FrozenDatetime)
    with client_for(build_app(empty=True), factory,
                    Principal("user_new", None, "operator")) as c:
        yield c


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> _FrozenDatetime:
        return cls(2026, 10, 3, 0, 0, tzinfo=tz)


CALLS: list[tuple[str, str, str, dict[str, Any] | None]] = [
    ("profile", "GET", "/api/v1/profile", None),
    ("exchange_accounts", "GET", "/api/v1/exchange-accounts", None),
    ("credentials", "GET", f"{BASE}/credentials", None),
    ("attribution_weekly", "GET", f"{BASE}/attribution/weekly", None),
    ("config_draft_get", "GET", f"{BASE}/config-draft", None),
    ("config_draft_put", "PUT", f"{BASE}/config-draft", PUT_BODY),
    ("funding_status", "GET", f"{BASE}/funding-status", None),
    ("positions", "GET", f"{BASE}/positions", None),
    ("offers", "GET", f"{BASE}/offers", None),
    ("executions", "GET", f"{BASE}/executions", None),
    ("public_proof_summary", "GET", "/api/v1/public/proof-summary", None),
    ("trading_control", "GET", f"{BASE}/trading-control", None),
    ("kill", "POST", f"{BASE}/trading-control/kill", {"reason": "venue incident"}),
    ("currency_disable", "POST", f"{BASE}/trading-control/currencies/fUST/disable",
     {"reason": "pause fUST"}),
    ("currency_request", "GET", f"{BASE}/trading-control/currency-requests/{CURRENCY_REQUEST}",
     None),
    ("trading_request", "GET", f"{BASE}/trading-control/requests/{KILL_REQUEST}", None),
    ("uncertainties", "GET", f"{BASE}/uncertainties", None),
    ("uncertainty", "GET", f"{BASE}/uncertainties/{UNCERTAINTY}", None),
    ("resolution_request", "GET",
     f"{BASE}/uncertainty-resolution-requests/{RESOLUTION_REQUEST}", None),
]


EMPTY_CALLS: list[tuple[str, str, str, dict[str, Any] | None]] = [
    ("profile_without_email", "GET", "/api/v1/profile", None),
    ("exchange_accounts_empty", "GET", "/api/v1/exchange-accounts", None),
    ("credentials_empty", "GET", f"{BASE}/credentials", None),
    ("attribution_weekly_empty", "GET", f"{BASE}/attribution/weekly", None),
    ("positions_empty", "GET", f"{BASE}/positions", None),
    ("offers_empty", "GET", f"{BASE}/offers", None),
    ("executions_empty", "GET", f"{BASE}/executions", None),
    ("public_proof_summary_empty", "GET", "/api/v1/public/proof-summary", None),
    ("trading_control_empty", "GET", f"{BASE}/trading-control", None),
    ("uncertainties_empty", "GET", f"{BASE}/uncertainties", None),
]


def canonical(value: object) -> str:
    """Key order aside, the exact JSON: 1 and 1.0, "1" and 1 and null all differ."""
    return json.dumps(value, sort_keys=True, indent=1)


def _api_routes(app: FastAPI) -> set[tuple[str, str]]:
    """(method, path template) of every /api/v1 operation the app serves (its OpenAPI
    paths: FastAPI includes routers lazily, so ``app.routes`` does not list them)."""
    return {(method.upper(), path) for path, item in app.openapi()["paths"].items()
            if path.startswith("/api/v1/") for method in item}


def _template(method: str, path: str, routes: set[tuple[str, str]]) -> tuple[str, str]:
    (route,) = [(m, t) for m, t in routes
                if m == method and re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", t), path)]
    return route


def test_every_api_route_is_called_here() -> None:
    """A new /api/v1 JSON route of the production app has to join the golden set, and the
    app these tests call serves exactly the production routes, refusals included."""
    from bfx_funding_bot.apps.webapi import app as production

    routes = _api_routes(production)
    assert _api_routes(build_app()) == routes
    assert ValidationError in production.exception_handlers
    assert ResponseValidationError in production.exception_handlers
    not_json = {("DELETE", "/api/v1/exchange-accounts/{exchange_account_id}/config-draft"),
                ("GET", "/api/v1/public/funding-rates.csv")}
    called = {_template(method, path, routes) for _, method, path, _ in CALLS}
    assert len(called) == len(CALLS)
    assert called == routes - not_json


def _matches_golden(client: TestClient, calls: list[tuple[str, str, str, dict[str, Any] | None]],
                    subtests: Any) -> None:
    golden = json.loads(GOLDEN.read_text())
    for name, method, path, body in calls:
        with subtests.test(msg=name):
            response = client.request(method, path, json=body)
            assert response.status_code == golden[name]["status"], response.text
            assert canonical(response.json()) == canonical(golden[name]["body"])


def test_the_golden_file_holds_exactly_these_calls() -> None:
    assert set(json.loads(GOLDEN.read_text())) == {name for name, *_ in CALLS + EMPTY_CALLS}


def test_every_response_matches_the_golden_wire_format(client: TestClient, subtests: Any) -> None:
    _matches_golden(client, CALLS, subtests)


def test_every_empty_response_matches_the_golden_wire_format(
        empty_client: TestClient, subtests: Any) -> None:
    _matches_golden(empty_client, EMPTY_CALLS, subtests)


def test_a_shape_the_console_could_misread_is_refused_not_served(
        client: TestClient, monkeypatch: pytest.MonkeyPatch, subtests: Any,
        caplog: pytest.LogCaptureFixture) -> None:
    """A value outside a declared union fails the response; the log names the route, the
    model and the field, never the value (no traceback repeats it)."""

    async def overview(*args: Any) -> dict[str, Any]:
        return _overview(phase="cancelled")

    async def offers(self: Any, session: Any, scope: Any, *, states: Any) -> tuple[OfferView, ...]:
        return (OfferView("o-1", "v1", "expired", "fUST", Decimal("1"), 1, 1),)

    async def request(*args: Any) -> UncertaintyResolutionRequestRow:
        return _resolution_request(state="superseded")

    async def uncertainty(*args: Any) -> UncertaintyDetail:
        detail = _uncertainty()
        view = detail.view
        return UncertaintyDetail(
            view=UncertaintyView(view.uncertainty_id, "venue_said_maybe", view.symbol, view.state,
                                 view.intended_amount, view.evidence),
            context=detail.context, latest_request=None)

    monkeypatch.setattr(operator_commands, "trading_overview", overview)
    monkeypatch.setattr(_Reads, "list_offers", offers)
    monkeypatch.setattr(operator_commands, "get_resolution_request", request)
    monkeypatch.setattr(operator_commands, "get_uncertainty", uncertainty)
    for name, path, status, field in (
        ("cancel-all phase", f"{BASE}/trading-control", 500, "cancel_all.0.phase"),
        ("offer state", f"{BASE}/offers", 500, "state"),
        ("request state", f"{BASE}/uncertainty-resolution-requests/{RESOLUTION_REQUEST}", 500,
         "state"),
        # An uncertainty that cannot be rendered is unavailable (_rendered), never a 500.
        ("uncertainty kind", f"{BASE}/uncertainties/{UNCERTAINTY}", 503, None),
    ):
        with subtests.test(msg=name):
            caplog.clear()
            response = client.get(path)
            assert response.status_code == status
            for leaked in ("cancelled", "expired", "superseded", "venue_said_maybe"):
                assert leaked not in response.text
                assert leaked not in caplog.text
            if field is not None:
                assert response.json() == {"detail": "response_shape_mismatch"}
                (record,) = [r for r in caplog.records
                             if r.getMessage().startswith("response_shape_mismatch")]
                assert field in record.getMessage() and record.exc_info is None


def test_a_returned_value_fastapi_refuses_is_logged_by_field_not_value(
        caplog: pytest.LogCaptureFixture) -> None:
    """FastAPI's own check of a handler's return value (``ResponseValidationError``, not a
    pydantic ``ValidationError``) takes the same path: field paths, no values, no traceback."""
    app = FastAPI()
    install_response_shape_handler(app)

    @app.get("/api/v1/probe")
    async def probe() -> OfferClaimResponse:
        refused: Any = {"state": "venue_said_maybe"}
        return refused

    with TestClient(app, raise_server_exceptions=False) as c:
        response = c.get("/api/v1/probe")

    assert response.status_code == 500
    assert response.json() == {"detail": "response_shape_mismatch"}
    (record,) = [r for r in caplog.records
                 if r.getMessage().startswith("response_shape_mismatch")]
    assert "/api/v1/probe" in record.getMessage() and "state" in record.getMessage()
    assert record.exc_info is None and "venue_said_maybe" not in caplog.text


@pytest.mark.parametrize("mutate", ["rename", "retype", "drop"])
def test_a_daemon_status_of_another_shape_is_unavailable(
        factory: async_sessionmaker[Any], patched_ports: None, mutate: str,
        caplog: pytest.LogCaptureFixture) -> None:
    """The daemon's body is forwarded only once it parses: refused like an unreachable daemon,
    logged by field path without its value."""

    async def daemon(path: str) -> dict[str, Any]:
        if path != "/admin/trading-status":
            return funding_dry_run()
        body = funding_snapshot()
        fust = body["symbols"]["fUST"]
        if mutate == "rename":
            fust["spendable_amount"] = fust.pop("spendable")
        elif mutate == "retype":
            fust["spendable"] = 1100.5
        else:
            del body["halt"]["halted"]
        return body

    with client_for(build_app(funding_reader=daemon), factory) as c:
        response = c.get(f"{BASE}/funding-status")
    assert response.status_code == 503
    assert response.json() == {"detail": "funding_status_unavailable"}
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "funding_status_shape_mismatch" in logged
    assert "1100.5" not in logged and "9007199254740993" not in logged
