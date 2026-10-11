"""The periodic deployment under the ledger, on a clone PostgreSQL database.

Mutations (apply one at a time, run this file, revert):

4. the input includes a foreign offer: ``test_the_input_is_the_managed_offers_with_their_terms``.
5. it includes a provenance-conflict offer: same test.
6. it includes a rate-unobserved offer: same test.
7. a conflict on symbol A stops symbol B: ``test_a_provenance_conflict_stops_only_its_symbol``.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.modules.execution.deployment import reconciler as reconciler_module
from bfx_funding_bot.modules.execution.deployment.reconciler import DeploymentReconciler
from bfx_funding_bot.modules.execution.deployment_input import LedgerDeploymentInput
from bfx_funding_bot.modules.execution.managed_cancel import ManagedOfferSweep
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.ledger.wiring import (
    build_ledger_managed_offers,
    build_managed_offer_reader,
)
from bfx_funding_bot.modules.observability import alerts
from tests.modules.execution.deployment.test_reconciler import _cell

from .test_ledger_basis import _observation, _offer
from .test_ledger_capital_reader import Book
from .test_ledger_capital_reader import book as book
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def ledger(book: Book):
    await book.accept()
    return book


def _input(book: Book) -> LedgerDeploymentInput:
    return LedgerDeploymentInput(
        session_factory=book.factory, account_id=book.scope.exchange_account_id,
        environment=book.scope.deployment_environment, offers=build_ledger_managed_offers(),
    )


@pytest.mark.asyncio
async def test_the_input_is_the_managed_offers_with_their_terms(ledger: Book) -> None:
    await ledger.accept(_observation(usd=True))
    await ledger.attempt("200", venue_offer_id="managed")
    await ledger.attempt("50", venue_offer_id="blind")
    await ledger.attempt("5", venue_offer_id="mixed-up", symbol="fUSD")
    unobserved = replace(_offer("blind", "50"), rate=None, rate_observed=False)
    await ledger.accept(_observation(
        "700", usd=True,
        offers=(
            _offer("managed", "200", "150"), _offer("manual", "300"), unobserved,
            _offer("mixed-up", "5"),   # placed as fUSD, mirrored as fUST: contradictory
        ),
    ))
    offers = await _input(ledger).offers(SimpleNamespace(decision="accepted"))  # type: ignore[arg-type]
    assert offers == (
        ActiveFundingOffer(
            "managed", "fUST", Decimal("150"), 0.0001, 2, 101_000, "partially_filled",
            amount_original=Decimal("200"), rate_observed=True, rate_decimal=Decimal("0.0001"),
        ),
    )


@pytest.mark.asyncio
async def test_an_empty_mirror_has_nothing_to_reprice(ledger: Book) -> None:
    assert await _input(ledger).offers(SimpleNamespace(decision="accepted")) == ()  # type: ignore[arg-type]


class _Canceller:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def cancel(self, *, venue_offer_id, **_kwargs) -> None:
        self.cancelled.append(venue_offer_id)


@pytest.mark.asyncio
async def test_a_provenance_conflict_stops_only_its_symbol(ledger: Book, monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(
        reconciler_module.alerts, "emit", lambda event, **fields: sent.append((event, fields))
    )
    await ledger.accept(_observation(usd=True))
    await ledger.attempt("5", venue_offer_id="mixed-up", symbol="fUSD")
    await ledger.accept(_observation("800", usd=True, offers=(_offer("mixed-up", "5"),)))

    ctx = AccountContext(
        account_id=str(uuid4()), credentials=Credentials(api_key="k", api_secret="s"),
    )
    canceller = _Canceller()
    sized: list[str] = []

    async def evaluate_before_sizing(symbol, _ctx):
        sized.append(symbol)
        return SimpleNamespace(allowed=False, guard_name="test", reason="stop here")

    class _Capital:
        async def read_policy(self, _session, _scope, symbol):
            # fUST is disabled (its managed offers are swept); fUSD trades.
            return SimpleNamespace(policy=SimpleNamespace(enabled=symbol != "fUST"))

    planner = object.__new__(DeploymentReconciler)
    planner._scope = ledger.scope
    planner._session_factory = ledger.factory
    planner._capital = _Capital()
    planner._ctx = ctx
    planner._clock = lambda: 1
    planner._reprice = None
    planner._cells = [_cell("fUST", "30"), _cell("fUSD", "30")]
    planner._safety = SimpleNamespace(evaluate_before_sizing=evaluate_before_sizing)
    planner._conflict_alerted = set()
    planner._managed_sweep = ManagedOfferSweep(
        session_factory=ledger.factory, account_id=ledger.scope.exchange_account_id,
        environment=ledger.scope.deployment_environment, canceller=canceller, ctx=ctx,
        offers=build_managed_offer_reader(),
    )

    await planner.deploy()
    await planner.deploy()
    assert sized == ["fUSD", "fUSD"]       # fUST placed nothing, fUSD was still planned
    assert canceller.cancelled == []       # contradictory: neither managed nor foreign
    assert sent == [(alerts.PROVENANCE_CONFLICT, {"symbol": "fUST", "venue_offer_id": "mixed-up"})]
    assert planner._conflict_alerted == {("fUST", "mixed-up")}
