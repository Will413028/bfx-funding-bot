"""The legacy adapters answer the ledger facade's read ports exactly as the
consumers' own reads did (S1-3c1): same refusals, same scope, same rows."""

from __future__ import annotations

import contextlib
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from bfx_funding_bot.modules.execution.capital_policy_read import CapitalBlockedError
from bfx_funding_bot.modules.execution.event_store.tables import VenueOfferStateRow
from bfx_funding_bot.modules.execution.legacy_ports import (
    LegacyCapitalAuthority,
    LegacyManagedOffers,
    LegacyScopeLock,
    LegacyUncertaintyReader,
    legacy_snapshot_seq,
)
from bfx_funding_bot.modules.execution.safety.protection import CAPITAL_BLOCK_TRIGGERS
from bfx_funding_bot.modules.ledger import (
    CapitalBlocked,
    LiveManagedOffer,
    Scope,
)
from bfx_funding_bot.modules.trading import CapitalScope
from tests.integration.test_capital_command_boundary import (
    append_cancel_race_unknown,
    boundary,
    read_capital,
    status_reads,
)
from tests.integration.test_capital_repository import (
    capital_db as capital_db,
)
from tests.integration.test_capital_repository import (
    capital_engine as capital_engine,
)
from tests.integration.test_capital_repository import snapshot

ACCOUNT = UUID(int=7)


class _RefusingRepository:
    account_id = ACCOUNT
    environment = "ci"

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def read_capital(self, session, **kwargs):  # type: ignore[no-untyped-def]
        raise self.error


def _authority(error: Exception) -> LegacyCapitalAuthority:
    @contextlib.asynccontextmanager
    async def session():  # type: ignore[no-untyped-def]
        yield object()

    runtime = SimpleNamespace(repository=_RefusingRepository(error), session_factory=session)
    return LegacyCapitalAuthority(runtime)  # type: ignore[arg-type]


REASONS = sorted(
    {
        *CAPITAL_BLOCK_TRIGGERS,
        "snapshot_query_pending",
        "snapshot_stale",
        "snapshot_unavailable",
        "execution_unknown",
        "policy_unavailable",
        "invalid_policy: bad",
    }
)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", REASONS)
async def test_every_authority_refusal_is_a_blocked_read_with_its_own_reason(reason: str) -> None:
    read = await _authority(CapitalBlockedError(reason)).read(
        CapitalScope(ACCOUNT, "ci", "fUST", "fUST_a30"), now_ms=1
    )
    assert read == CapitalBlocked(reason)


@pytest.mark.asyncio
async def test_infrastructure_failures_still_raise_and_scope_is_checked() -> None:
    with pytest.raises(RuntimeError, match="db down"):
        await _authority(RuntimeError("db down")).read(
            CapitalScope(ACCOUNT, "ci", "fUST", "a30"), now_ms=1
        )
    authority = _authority(CapitalBlockedError("never"))
    for scope in (
        CapitalScope(uuid4(), "ci", "fUST", "a30"),
        CapitalScope(ACCOUNT, "shadow", "fUST", "a30"),
    ):
        with pytest.raises(ValueError, match="capital_scope_conflict"):
            await authority.read(scope, now_ms=1)


@pytest.mark.asyncio
async def test_basis_token_is_the_snapshot_seq_and_status_renders_it_unchanged(capital_db) -> None:
    """The token is opaque to consumers; the legacy one is the decimal snapshot_seq,
    so the status report preserves its decimal string."""
    from sqlalchemy import func, select

    from bfx_funding_bot.core.health import HealthProbe
    from bfx_funding_bot.core.telemetry import Phase
    from bfx_funding_bot.modules.admin.trading_status import TradingStatusService
    from bfx_funding_bot.modules.execution.capital_tables import CapitalSnapshotRow
    from bfx_funding_bot.modules.execution.deployment.submit_attempt import SubmitAttemptRecorder
    from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
    from bfx_funding_bot.modules.strategy import StrategyName
    from tests.modules.execution.deployment.test_reconciler import _CapturingSink, _cell

    factory, account = capital_db
    _, _, _, ctx, runtime, halt = await boundary(factory, account)
    await snapshot(factory, runtime.repository)  # a later basis: seq differs from revision 1
    async with factory() as session:
        seq = await session.scalar(select(func.max(CapitalSnapshotRow.event_seq)))
    assert seq is not None and seq > 1
    view = await read_capital(runtime, "a30")
    assert view.basis_token == str(seq)
    assert legacy_snapshot_seq(view.basis_token) == seq
    chain = SafetyGuardChain(
        guards=[],
        probe=HealthProbe(),
        diagnostics=_CapturingSink(),
        phase=Phase.LIVE,
        strategy=StrategyName.MEAN_REVERSION,
        cell="fUST_a30",
        account_id=str(account),
    )
    service = TradingStatusService(
        chain=chain,
        account_ctx=ctx,  # type: ignore[arg-type]
        cells=[_cell("fUST", "a30")],
        phase=Phase.LIVE,
        attempts=SubmitAttemptRecorder(),
        trading_state=halt,
        exposure=status_reads(runtime),
    )
    status = (await service.snapshot())["symbols"]["fUST"]
    assert status["basis_token"] == str(seq)
    assert "snapshot_seq" not in status


@pytest.mark.parametrize("token", ["042", "-1", "1.0", "", "x", "٤٢"])
def test_the_legacy_boundary_refuses_a_token_it_did_not_issue(token: str) -> None:
    with pytest.raises(CapitalBlockedError, match="snapshot_changed"):
        legacy_snapshot_seq(token)


@pytest.mark.asyncio
async def test_policy_read_returns_the_applied_revision_or_the_refusal(capital_db) -> None:
    factory, account = capital_db
    _, _, _, _, runtime, _ = await boundary(factory, account)
    authority = LegacyCapitalAuthority(runtime)
    scope = Scope(account, "ci")
    async with factory() as session:
        applied = await authority.read_policy(session, scope, "fUST")
        missing = await authority.read_policy(session, scope, "fUSD")
    assert not isinstance(applied, CapitalBlocked)
    assert (applied.account_id, applied.environment, applied.symbol, applied.revision) == (
        account,
        "ci",
        "fUST",
        1,
    )
    assert missing == CapitalBlocked("policy_unavailable")


@pytest.mark.asyncio
async def test_uncertainty_reads_are_exactly_account_environment_and_symbol_scoped(
    capital_db,
) -> None:
    factory, account = capital_db
    await append_cancel_race_unknown(factory, account, symbol="fUSD")
    await append_cancel_race_unknown(factory, account, symbol="fBTC", environment="shadow")
    await append_cancel_race_unknown(factory, uuid4(), symbol="fUST")
    reader = LegacyUncertaintyReader(factory)
    scope = Scope(account, "ci")
    assert await reader.has_open(None, scope, "fUSD") is True
    assert await reader.has_open(None, scope, "fUST") is False  # another symbol's UNKNOWN
    assert await reader.has_open(None, scope, "fBTC") is False  # another environment's
    async with factory() as session:
        assert await reader.has_open(session, scope, "fUSD") is True
        records = await reader.list_open(session, scope)
    assert [
        (r.symbol, r.kind, r.exchange_account_id, r.deployment_environment) for r in records
    ] == [("fUSD", "submit_outcome_unknown", account, "ci")]
    assert await reader.list_open(None, scope, "fUST") == ()
    assert await reader.list_open(None, Scope(account, "shadow"), "fBTC") != ()


@pytest.mark.asyncio
async def test_managed_offer_reads_count_only_live_managed_offers(capital_db) -> None:
    factory, account = capital_db

    async def add(
        offer_id: str,
        *,
        symbol: str = "fUST",
        terminal: bool = False,
        managed: bool = True,
        environment: str = "ci",
    ) -> None:
        async with factory.begin() as session:
            session.add(
                VenueOfferStateRow(
                    exchange_account_id=account,
                    deployment_environment=environment,
                    venue_offer_id=offer_id,
                    symbol=symbol,
                    amount_original=Decimal("200"),
                    amount_remaining=Decimal("200"),
                    rate=Decimal("0.0002"),
                    period_days=2,
                    status="ACTIVE",
                    flags={},
                    mts_created=1,
                    mts_updated=1,
                    first_seen_event_seq=1,
                    last_seen_event_seq=1,
                    is_terminal=terminal,
                    execution_decision_id=f"d-{offer_id}" if managed else None,
                    signal_correlation_id=f"s-{offer_id}" if managed else None,
                )
            )

    await add("2")
    await add("1")
    await add("m", managed=False)  # manual: never managed, still live
    # A symbol with only a manual offer: the kill switch must still cancel it.
    await add("n", symbol="fEUR", managed=False)
    await add("t", terminal=True)  # terminal: not live
    await add("u", symbol="fUSD")
    await add("x", symbol="fBTC", terminal=True, managed=False)
    await add("e", environment="shadow")  # another environment
    offers, scope = LegacyManagedOffers(), Scope(account, "ci")
    async with factory() as session:
        assert await offers.count_live(session, scope, "fUST") == 2
        assert await offers.count_live(session, scope, "fUSD") == 1
        assert await offers.live(session, scope, ["fUST"]) == (
            LiveManagedOffer("1", "fUST", "s-1"),
            LiveManagedOffer("2", "fUST", "s-2"),
        )
        assert [o.venue_offer_id for o in await offers.live(session, scope)] == ["1", "2", "u"]
        assert await offers.live_symbols(session, scope) == frozenset({"fUST", "fUSD", "fEUR"})
        assert await offers.fingerprints_in_use(session, scope, "fUST") == frozenset()


@pytest.mark.asyncio
async def test_scope_lock_refuses_another_scope(capital_db) -> None:
    from tests.integration.test_capital_repository import repository

    factory, account = capital_db
    lock = LegacyScopeLock(repository(account))
    async with factory.begin() as session:
        await lock.lock(session, Scope(account, "ci"))
        with pytest.raises(ValueError, match="capital_scope_conflict"):
            await lock.lock(session, Scope(account, "shadow"))


@pytest.mark.parametrize("token", ["042", " 42"])
def test_legacy_evidence_parser_rejects_noncanonical_decimal(token: str) -> None:
    from bfx_funding_bot.modules.execution.operator_evidence import legacy_evidence_seq
    from bfx_funding_bot.modules.ledger import ResolutionRejected

    with pytest.raises(ResolutionRejected, match="stale_reconcile_fence"):
        legacy_evidence_seq(token)
