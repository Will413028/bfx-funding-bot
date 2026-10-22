from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.modules.api.account_scope import ExchangeAccountContext, require_account_member
from tests.ingress_support import attach_ingress
from tests.modules.api.test_response_contract import funding_dry_run, funding_snapshot


@pytest.mark.parametrize("scope_error", [None, "account", "realm", "missing_realm"])
@pytest.mark.parametrize("scope_endpoint", ["/admin/trading-status", "/admin/dry-evaluate"])
async def test_funding_status_preserves_decimals_only_for_both_matching_scopes(scope_error, scope_endpoint):
    from bfx_funding_bot.modules.api.funding_status import build_funding_status_router

    account = uuid4()
    observed = funding_snapshot() | {"account_id": str(account)}
    observed["halt"] = observed["halt"] | {"halted": True}
    observed["symbols"]["fUST"]["spendable"] = "9007199254740993.123456789"
    paths = []
    async def read(path):
        paths.append(path)
        value = dict(observed) if path == "/admin/trading-status" else (
            funding_dry_run() | {"account_id": str(account)})
        if path == scope_endpoint:
            if scope_error == "account":
                value["account_id"] = str(uuid4())
            elif scope_error == "realm":
                value["deployment_environment"] = "ci"
            elif scope_error == "missing_realm":
                value.pop("deployment_environment")
        return value

    app = FastAPI()
    app.include_router(build_funding_status_router(reader=read))
    app.dependency_overrides[require_operator] = lambda: Principal("fixture", None, "admin")
    app.dependency_overrides[require_account_member] = lambda: ExchangeAccountContext(
        exchange_account_id=account, deployment_environment="prod", user_id="fixture", role="owner",
        lifecycle_status="active")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/exchange-accounts/{account}/funding-status")
    if scope_error:
        assert response.status_code == 503
        assert len(paths) == (1 if scope_endpoint == "/admin/trading-status" else 2)
        assert "9007199254740993" not in response.text
    else:
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["symbols"]["fUST"]["spendable"] == "9007199254740993.123456789"
        assert data["dry_run"]["symbols"]["fUST"]["blocked_by"] == "manual_kill"
        assert data["halt"]["halted"]


async def test_funding_status_has_no_anonymous_access():
    from bfx_funding_bot.modules.api.funding_status import build_funding_status_router

    async def read(path):
        pytest.fail("anonymous caller must not reach daemon")
    app = attach_ingress(FastAPI())
    app.include_router(build_funding_status_router(reader=read))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/exchange-accounts/{uuid4()}/funding-status")
    assert response.status_code == 401


async def test_the_daemons_own_status_parses_and_is_forwarded_unchanged():
    """The producer of the forwarded body is the daemon's TradingStatusService: its real
    snapshot and dry run parse as the console's model, and nothing is added or lost."""
    import json

    from bfx_funding_bot.modules.api.funding_status import FundingStatus
    from tests.modules.admin.test_trading_status import _FakeTradingState, _halt_state, _service

    service = _service(trading_state=_FakeTradingState(_halt_state(True)))
    body = json.loads(json.dumps({**await service.snapshot(), "dry_run": await service.dry_run()}))
    status = FundingStatus.model_validate(body)
    assert status.halt.halted is True
    assert status.model_dump(mode="json", by_alias=True, exclude_unset=True) == body
