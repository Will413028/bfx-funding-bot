from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from bfx_funding_bot.modules.accounts.tables import AccountConfigDraft
from bfx_funding_bot.modules.execution.capital_tables import (
    CapitalPolicyRevisionRow,
    CapitalSnapshotRow,
)
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.events import SnapshotCoverage, VenueSnapshotObserved
from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
from tests.integration.test_capital_repository import (
    capital_db,
    capital_engine,
    repository,
    snapshot,
)

# Re-export real DB fixtures; PG variants retain the integration marker.
__all__ = ["capital_db", "capital_engine"]


async def currency_snapshot(factory, repo, **overrides):
    """Accept immutable evidence with the wallet/coverage shape under test."""
    async with factory.begin() as session:
        fence = await repo.begin_snapshot(session, now_ms=1000)
    event = replace(VenueSnapshotObserved(
        account_id=str(repo.account_id), environment=repo.environment,
        query_started_at_ms=1000, query_finished_at_ms=1050,
        offers=(), credits=(), wallet_available={"fUST": Decimal("1000")},
        coverage=SnapshotCoverage(True, True, True),
    ), **overrides)
    async with factory.begin() as session:
        result = await repo.accept_snapshot(session, fence=fence, event=event,
            confirmation=replace(event, query_started_at_ms=1050, query_finished_at_ms=1060,
                                 event_id=uuid4()), now_ms=1060)
        assert result.event_seq is not None
        return result.event_seq


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["pending", "offer", "shared_credit"])
async def test_conversion_matches_canonical_runtime_exposure(capital_db, kind):
    from dataclasses import replace

    from bfx_funding_bot.modules.accounts.capital_conversion import convert_capital_policy
    from bfx_funding_bot.modules.execution.capital_runtime import CapitalRuntime
    from bfx_funding_bot.modules.execution.event_store.entities import (
        VenueCreditObservation,
        VenueOfferObservation,
    )
    from bfx_funding_bot.modules.execution.events import ReservationClaimed
    from tests.integration.test_capital_repository import intent, setup_policy, simulated_guard

    factory, account = capital_db
    repo = repository(account)
    policy = await setup_policy(factory, repo, reserve="0", fraction="0.70")
    seq = await snapshot(factory, repo)
    if kind != "shared_credit":
        event, decision = intent(account, "600")
        decision.cell_id = "fUST_a30"
        async with factory.begin() as session:
            result = await repo.authorize_and_append_intent(
                session, intent=event, decision=decision, expected_revision=policy.revision,
                expected_digest=policy.digest, expected_snapshot_seq=seq, now_ms=1100,
                locked_guard=simulated_guard)
            if kind == "offer":
                await repo.writer.append(session, ReservationClaimed(
                    symbol="fUST", cid=1, signal_correlation_id=event.signal_correlation_id,
                    account_id=str(account), is_simulated=True, amount=Decimal("600"),
                    venue_offer_id="offer-1", occurred_at_ms=1100,
                    reservation_ref=replace(result.intent.reservation_ref, venue_offer_id="offer-1")))
        if kind == "offer":
            await snapshot(factory, repo, "400", offers=(VenueOfferObservation(
                "offer-1", "fUST", Decimal("600"), Decimal("600"), Decimal("0.0001"),
                2, "active", 1000, 1100),))
    else:
        await snapshot(factory, repo, "400", credits=(VenueCreditObservation(
            "credit-1", "fUST", Decimal("600"), Decimal("0.0001"), 2, "active"),))
    runtime = CapitalRuntime(repository=repo, session_factory=factory, clock=lambda: 1100)
    async with factory.begin() as session:
        report = await convert_capital_policy(session, repository=repo, legacy=legacy(),
                                              now_ms=1100, apply_digest=None)
    for cell in ("fUST_a30", "fUST_p2"):
        view = await runtime.read(symbol="fUST", cell_id=cell)
        values = report["symbols"]["fUST"]["cells"][cell]
        # An unattributed credit is in T only, so no cell carries it.
        expected_exposure = Decimal("600") if cell == "fUST_a30" and kind != "shared_credit" else Decimal("0")
        assert Decimal(values["cell_exposure"]) == expected_exposure == view.snapshot.cell_exposure
        assert Decimal(values["unattributed_credit_exposure"]) == view.unattributed_credit_exposure == (
            Decimal("600") if kind == "shared_credit" else Decimal("0"))
        assert Decimal(values["new_max_new_offer"]) == view.budget.max_new_offer
        assert Decimal(values["new_cell_headroom"]) == view.budget.cell_headroom
    # shared_credit: cash (400) binds under the 700 cap; otherwise a30's own 600 does.
    expected_max = Decimal("400") if kind == "shared_credit" else Decimal("100")
    assert Decimal(report["symbols"]["fUST"]["cells"]["fUST_a30"]["new_max_new_offer"]) == expected_max


def legacy():
    return {"schema_version": 1, "caps": {"fUST": "200", "fUSD": "0"},
            "default_cap": "0", "env_fallback_cap": "400",
            "buffers": {"fUST": "3", "fUSD": "0"}, "default_buffer": "0",
            "env_fallback_buffer": "3", "max_cell_fraction": "0.70"}


@pytest.mark.asyncio
@pytest.mark.parametrize("usd_balance", [None, "0", "250"], ids=["ust_only", "usd_zero", "usd_balance"])
async def test_preview_apply_preserves_draft_and_history_and_never_resumes(capital_db, usd_balance):
    from bfx_funding_bot.modules.accounts.capital_conversion import convert_capital_policy
    factory, account = capital_db
    repo = repository(account)
    wallets = {"fUST": Decimal("1000")}
    if usd_balance is not None:
        wallets["fUSD"] = Decimal(usd_balance)
    seq = await currency_snapshot(factory, repo, wallet_available=wallets)
    trading = TradingStateRepository(factory, account_id=account, deployment_environment="ci")
    await trading.transition("HALTED", cause="operator", actor="test", reason="test", now_ms=1)
    async with factory.begin() as session:
        session.add(AccountConfigDraft(exchange_account_id=account, config={"currency": "UST"},
                                       revision=7, source="operator"))
    async with factory.begin() as session:
        report = await convert_capital_policy(session, repository=repo, legacy=legacy(),
                                              now_ms=1100, apply_digest=None)
        assert report["symbols"]["fUST"]["new_policy"]["reserve_amount"] == "0"
        assert report["symbols"]["fUSD"]["new_policy"]["enabled"] is False
        for cell in ("fUSD_a30", "fUSD_p2"):
            assert report["symbols"]["fUSD"]["cells"][cell] == {
                "status": "disabled", "capital_evaluated": False,
            }
        stored = await session.get(CapitalSnapshotRow, seq)
        assert set(stored.classification["symbols"]) == set(wallets)
        logged = await session.get(EventLogRow, seq)
        assert set(logged.payload["wallet_available"]) == set(wallets)
        assert report["symbols"]["fUST"]["cells"]["fUST_a30"]["snapshot_seq"] == seq
        assert report["symbols"]["fUST"]["old_effective"]["cap"] == "200"
        assert report["symbols"]["fUST"]["cells"]["fUST_a30"]["new_max_new_offer"] == "700.00"
        assert report["symbols"]["fUST"]["cells"]["fUST_a30"]["delta"] == "500.00"
        assert await session.scalar(select(func.count()).select_from(CapitalPolicyRevisionRow)) == 0
    async with factory.begin() as session:
        applied = await convert_capital_policy(session, repository=repo, legacy=legacy(),
            now_ms=1100, apply_digest=report["conversion_digest"])
        assert applied["status"] == "applied"
        assert applied["conversion_digest"] == report["conversion_digest"]
        assert (await repo.read_applied(session, symbol="fUSD")).policy.enabled is False
        assert (await repo.read_applied(session, symbol="fUST")).policy.reserve_amount == Decimal("0")
        assert (await session.scalar(select(AccountConfigDraft))).revision == 7
    assert (await trading.current()).state == "HALTED"  # converting never resumes
    async with factory.begin() as session:
        again = await convert_capital_policy(session, repository=repo, legacy=legacy(),
            now_ms=1100, apply_digest=report["conversion_digest"])
        assert again["status"] == "already_applied"
        assert await session.scalar(select(func.count()).select_from(CapitalPolicyRevisionRow)) == 2
        stored = await session.get(CapitalSnapshotRow, seq)
        assert set(stored.classification["symbols"]) == set(wallets)


async def _open_unknown(factory, repo, account, symbol):
    from bfx_funding_bot.modules.execution.events import ReservationUnknown
    from tests.integration.test_capital_repository import intent

    event, decision = intent(account, symbol=symbol)
    async with factory.begin() as session:
        session.add(decision)
        await session.flush()
        await repo.writer.append(session, event)
        await repo.writer.append(session, ReservationUnknown(
            symbol=symbol, cid=event.cid, account_id=str(account), is_simulated=True,
            signal_correlation_id=event.signal_correlation_id,
            reservation_ref=event.reservation_ref, amount=event.amount,
            reason="test-outcome", occurred_at_ms=1150,
        ))


@pytest.mark.asyncio
async def test_an_unknown_on_the_disabled_currency_withholds_only_that_currency(capital_db):
    """Ladder level 2 is per symbol (lending envelope T2): an UNKNOWN fUSD
    submit says nothing about fUST's wallet, so fUST is still evaluated."""
    from bfx_funding_bot.modules.accounts.capital_conversion import convert_capital_policy

    factory, account = capital_db
    repo = repository(account)
    await currency_snapshot(factory, repo, wallet_available={"fUST": Decimal("1000")})
    await _open_unknown(factory, repo, account, "fUSD")
    async with factory.begin() as session:
        report = await convert_capital_policy(session, repository=repo, legacy=legacy(),
                                              now_ms=1200, apply_digest=None)
    for cell in ("fUST_a30", "fUST_p2"):
        assert "unavailable" not in report["symbols"]["fUST"]["cells"][cell]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,reason", [
    ("missing_ust", "snapshot_symbol_missing"),
    ("stale", "snapshot_stale"),
    ("incomplete", "snapshot_unavailable"),
    ("unknown_ust", "execution_unknown"),
])
async def test_disabled_currency_does_not_bypass_enabled_or_account_guards(capital_db, fault, reason):
    from bfx_funding_bot.modules.accounts.capital_conversion import convert_capital_policy
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError

    factory, account = capital_db
    repo = repository(account)
    if fault == "incomplete":
        with pytest.raises(CapitalBlockedError, match="snapshot_incomplete"):
            await currency_snapshot(factory, repo, coverage=SnapshotCoverage(True, False, True))
    else:
        wallets = {"fUSD": Decimal("0")} if fault == "missing_ust" else {"fUST": Decimal("1000")}
        await currency_snapshot(factory, repo, wallet_available=wallets)
    if fault == "unknown_ust":
        await _open_unknown(factory, repo, account, "fUST")
    now_ms = 11001 if fault == "stale" else 1200
    async with factory.begin() as session:
        report = await convert_capital_policy(session, repository=repo, legacy=legacy(),
                                              now_ms=now_ms, apply_digest=None)
        for cell in ("fUST_a30", "fUST_p2"):
            assert report["symbols"]["fUST"]["cells"][cell] == {"unavailable": reason}
        assert report["symbols"]["fUSD"]["cells"]["fUSD_a30"] == {
            "status": "disabled", "capital_evaluated": False,
        }
    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match="conversion_snapshot_unavailable"):
            await convert_capital_policy(session, repository=repo, legacy=legacy(),
                now_ms=now_ms, apply_digest=report["conversion_digest"])
        assert await session.scalar(select(func.count()).select_from(CapitalPolicyRevisionRow)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["snapshot", "legacy", "policy"])
async def test_ust_only_conversion_digest_binds_enabled_evidence_and_sources(capital_db, changed):
    from bfx_funding_bot.modules.accounts.capital_conversion import convert_capital_policy
    from bfx_funding_bot.modules.execution.capital_policy import CapitalPolicy
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError

    factory, account = capital_db
    repo = repository(account)
    await currency_snapshot(factory, repo)
    source = legacy()
    async with factory.begin() as session:
        report = await convert_capital_policy(session, repository=repo, legacy=source,
                                              now_ms=1100, apply_digest=None)
    if changed == "snapshot":
        await currency_snapshot(factory, repo)
    elif changed == "legacy":
        source["caps"]["fUST"] = "201"
    else:
        async with factory.begin() as session:
            await repo.apply_policy(session, symbol="fUST", policy=CapitalPolicy(enabled=True),
                                    expected_revision=0, source={"operator": "test"})
    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match="conversion_changed"):
            await convert_capital_policy(session, repository=repo, legacy=source,
                now_ms=1100, apply_digest=report["conversion_digest"])
        assert await session.scalar(select(func.count()).select_from(CapitalPolicyRevisionRow)) == (
            1 if changed == "policy" else 0
        )


@pytest.mark.asyncio
async def test_invalid_legacy_and_stale_draft_do_not_apply(capital_db):
    from bfx_funding_bot.modules.accounts.capital_conversion import convert_capital_policy
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    factory, account = capital_db
    repo = repository(account)
    await snapshot(factory, repo)
    bad = legacy()
    bad["caps"] = {"fUST": "NaN"}
    async with factory.begin() as session:
        report = await convert_capital_policy(session, repository=repo, legacy=bad,
                                              now_ms=1100, apply_digest=None)
        assert report["invalid_items"] == ["caps.fUST"]
        assert report["status"] == "invalid"
    async with factory.begin() as session:
        report = await convert_capital_policy(session, repository=repo, legacy=legacy(),
                                              now_ms=1100, apply_digest=None)
        session.add(AccountConfigDraft(exchange_account_id=account, config={"currency": "USD"},
                                       revision=1, source="operator"))
    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match="conversion_changed"):
            await convert_capital_policy(session, repository=repo, legacy=legacy(),
                now_ms=1100, apply_digest=report["conversion_digest"])
