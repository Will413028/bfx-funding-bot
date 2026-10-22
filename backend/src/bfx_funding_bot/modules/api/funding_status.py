"""Authenticated account adapter for the daemon's canonical read-only status.

No budget calculation or execution endpoint exists in this adapter. It holds the
daemon's read token only (``BFX_ADMIN_READ_TOKEN``), which the daemon refuses for
halt; user mutations use durable release sessions.

The daemon's ``/admin/trading-status`` and ``/admin/dry-evaluate`` bodies are forwarded
as they are, but only once they parse as ``FundingStatus``: the fields the console reads
are typed (the daemon builds them in ``modules/admin/trading_status.py``), the rest passes
through untouched (``extra="allow"``). A body the console could misread is refused like
an unreachable daemon, never forwarded.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import ConfigDict, ValidationError

from bfx_funding_bot.core.secret_files import read_secret
from bfx_funding_bot.modules.api.account_scope import ExchangeAccountContext, require_account_member
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency
from bfx_funding_bot.modules.api.schemas import Envelope, ResponseModel, field_paths

logger = logging.getLogger(__name__)


class _Forwarded(ResponseModel):
    """A daemon object: its typed fields are checked, the others forwarded as sent."""

    model_config = ConfigDict(extra="allow")


class CapitalPolicyStatus(_Forwarded):
    enabled: bool
    reserve_amount: str
    allocation_mode: Literal["all_available"]
    max_cell_fraction: str


class CapitalBudgetStatus(_Forwarded):
    spendable: str
    cell_limit: str
    cell_headroom: str
    max_new_offer: str
    reason: str | None


class CapitalUnavailable(_Forwarded):
    # The daemon names the policy only when one is applied: absent, not null, otherwise
    # (the route excludes unset fields, so an absent one stays absent).
    model_config = ConfigDict(json_schema_serialization_defaults_required=False)

    capital_available: Literal[False]
    reason: str
    policy_revision: int | None = None
    policy: CapitalPolicyStatus | None = None


class CapitalAvailable(_Forwarded):
    capital_available: Literal[True]
    policy_revision: int
    policy_digest: str
    basis_token: str
    policy: CapitalPolicyStatus
    available_balance: str
    unreflected_commitments: str
    total_capital: str
    spendable: str
    unattributed_credit_exposure: str
    cells: dict[str, CapitalBudgetStatus]


class HaltStatus(_Forwarded):
    halted: bool
    reason: str | None
    sources: dict[str, Any]


class ConfiguredCell(_Forwarded):
    symbol: str
    cell: str
    strategy: str
    period: str


class DryRunCell(_Forwarded):
    would_submit: bool
    blocked_by: str | None


class DryRunSymbol(_Forwarded):
    blocked_by: str | None
    cells: dict[str, DryRunCell]


class DryRun(_Forwarded):
    account_id: str
    deployment_environment: str
    symbols: dict[str, DryRunSymbol]


class FundingStatus(_Forwarded):
    """``/admin/trading-status`` with the dry run of ``/admin/dry-evaluate`` under ``dry_run``."""

    account_id: str
    deployment_environment: str
    halt: HaltStatus
    symbols: dict[str, CapitalUnavailable | CapitalAvailable]
    configured_cells: list[ConfiguredCell]
    dry_run: DryRun


async def _read_daemon(path: str) -> dict[str, Any]:
    # The web API's secret file in production (BFX_SECRETS_DIR; missing or empty raises,
    # answered 503 below like any failure), else the env.
    token = read_secret("BFX_ADMIN_READ_TOKEN")
    if token is None:
        raise RuntimeError("daemon_status_unconfigured")
    async with httpx.AsyncClient(base_url="http://bfx-bot:8080", timeout=15,
                                follow_redirects=False, trust_env=False) as client:
        response = await client.request("GET" if path.endswith("trading-status") else "POST",
            path, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        value: dict[str, Any] = response.json()
        return value


def build_funding_status_router(*,
    reader: Callable[[str], Awaitable[dict[str, Any]]] = _read_daemon,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/exchange-accounts/{exchange_account_id}",
        tags=["funding"], dependencies=[Depends(shared_rate_limit_dependency())])

    @router.get("/funding-status", response_model_exclude_unset=True)
    async def funding_status(
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
    ) -> Envelope[FundingStatus]:
        def require_scope(value: dict[str, Any]) -> None:
            if (value.get("account_id") != str(context.exchange_account_id)
                    or value.get("deployment_environment") != context.deployment_environment):
                raise RuntimeError("daemon_scope_mismatch")

        try:
            snapshot = await reader("/admin/trading-status")
            require_scope(snapshot)
            dry_run = await reader("/admin/dry-evaluate")
            require_scope(dry_run)
            return Envelope(data=FundingStatus.model_validate({**snapshot, "dry_run": dry_run}))
        except ValidationError as exc:
            # The daemon and this adapter ship in one image, so this is a build whose two
            # halves disagree: logged (field paths only, never the values) and refused.
            logger.error("funding_status_shape_mismatch: %s", field_paths(exc))
            raise HTTPException(status_code=503, detail="funding_status_unavailable") from None
        except Exception:
            # Never return internal URLs, credentials, a different account, or cached amounts.
            raise HTTPException(status_code=503, detail="funding_status_unavailable") from None

    return router
