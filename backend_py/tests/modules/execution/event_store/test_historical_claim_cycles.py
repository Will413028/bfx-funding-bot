"""Persisted synthetic lifecycle evidence, never production event fixtures."""
from copy import deepcopy
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.canonical import canonical_event_hash
from bfx_funding_bot.modules.execution.event_store.historical_claims import (
    historical_claim_reset_sequences,
)
from bfx_funding_bot.modules.execution.event_store.serialization import serialize_event
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    PositionStateRow,
)
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationIntent,
)
from tests.modules.execution.event_store.test_store_append_unit import _create_all

ACCOUNT = UUID("550e8400-e29b-41d4-a716-446655440081")
SIGNAL = UUID("11111111-1111-1111-1111-111111111181")


def historical_rows(*, first_amount: str = "5", environment: str = "prod") -> list[EventLogRow]:
    rows = []
    for venue, amount in (("old-a", first_amount), ("old-b", "5")):
        for kind in ("RESERVATION_INTENT", "RESERVATION_CLAIMED", "ORDER_FILL"):
            seq = len(rows) + 1
            binding = None if kind == "RESERVATION_INTENT" else venue
            payload = {
                "cid": 81, "account_id": str(ACCOUNT), "symbol": "fUST",
                "signal_correlation_id": str(SIGNAL), "amount": amount,
                "size_usdt": amount, "is_simulated": False,
                "venue_offer_id": binding, "venue_seq": seq if binding else None,
                "occurred_at_ms": seq * 1000, "fill_rate": 0.001,
                "credit_id": None,
            }
            rows.append(EventLogRow(
                account_id=str(ACCOUNT), exchange_account_id=ACCOUNT,
                deployment_environment=environment, event_type=kind, cid=81,
                venue_offer_id=binding, venue_seq=payload["venue_seq"],
                payload=payload, occurred_at_ms=seq * 1000,
            ))
    return rows


@pytest.mark.parametrize("symbol", [None, "fUST"])
@pytest.mark.parametrize("first_amount,realized", [("5", "10"), ("7", "12")])
async def test_completed_cycles_rebuild_without_losing_fills_or_mutating_events(
    sqlite_session: AsyncSession, symbol: str | None, first_amount: str, realized: str,
) -> None:
    await _create_all(sqlite_session)
    rows = historical_rows(first_amount=first_amount)
    payloads = deepcopy([row.payload for row in rows])
    sqlite_session.add_all(rows)
    await sqlite_session.flush()
    before_hash = canonical_event_hash(rows)
    store = PostgresEventStore(deployment_environment="prod")
    snapshots = []
    for _ in range(2):
        await store.rebuild_snapshot_from_log(
            sqlite_session, account_id=str(ACCOUNT), deployment_environment="prod", symbol=symbol,
        )
        await sqlite_session.flush()
        claim = (await sqlite_session.scalars(select(OfferClaimRow))).one()
        position = (await sqlite_session.scalars(select(PositionStateRow))).one()
        assert (claim.state, claim.venue_offer_id, claim.size_usdt) == (
            "released", "old-b", Decimal("5"),
        )
        assert (position.reserved, position.realized) == (Decimal("0"), Decimal(realized))
        if symbol is None:
            assert (position.offered_amount, position.lent_amount) == (0, Decimal(realized))
        snapshots.append((claim.last_event_seq, claim.occurred_at_ms, claim.last_updated_ms,
                          position.last_event_seq, position.last_updated_ms,
                          position.reserved, position.realized))
        stored = (await sqlite_session.scalars(select(EventLogRow).order_by(EventLogRow.event_seq))).all()
        assert len(stored) == 6
        assert canonical_event_hash(stored) == before_hash
        assert [row.payload for row in stored] == payloads
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize("symbol", [None, "fUST"])
@pytest.mark.parametrize("fault", [
    "active", "unknown", "missing_intent", "missing_first_intent", "venue",
    "amount", "partial_fill", "symbol", "signal", "stale", "duplicate_venue",
    "modern_mix", "v3_mix", "forged_flag", "row_cid", "row_venue", "payload_account",
])
async def test_invalid_stream_is_rejected_before_projection_deletion(
    sqlite_session: AsyncSession, symbol: str | None, fault: str,
) -> None:
    await _create_all(sqlite_session)
    rows = historical_rows()
    if fault == "active":
        del rows[2]
    elif fault == "unknown":
        rows[2].event_type = "SUBMIT_OUTCOME_UNKNOWN"
    elif fault == "missing_intent":
        del rows[3]
    elif fault == "missing_first_intent":
        del rows[0]
    elif fault == "venue":
        rows[2].venue_offer_id = rows[2].payload["venue_offer_id"] = "contradiction"
    elif fault in {"amount", "partial_fill"}:
        row = rows[1 if fault == "amount" else 2]
        row.payload.update(amount="2", size_usdt="2")
    elif fault == "symbol":
        rows[2].payload["symbol"] = "fUSD"
    elif fault == "signal":
        rows[2].payload["signal_correlation_id"] = str(UUID(int=99))
    elif fault == "stale":
        stale = historical_rows()[2]
        stale.venue_seq = stale.payload["venue_seq"] = 99
        rows.append(stale)
    elif fault == "duplicate_venue":
        for row in rows[3:]:
            row.cid = row.payload["cid"] = 82
            if row.venue_offer_id:
                row.venue_offer_id = row.payload["venue_offer_id"] = "old-a"
    elif fault == "modern_mix":
        rows[3].payload["execution_decision_id"] = "audited-modern-decision"
    elif fault == "v3_mix":
        intent = ReservationIntent(
            cid=81, symbol="fUST", amount=Decimal("5"), signal_correlation_id=SIGNAL,
            account_id=str(ACCOUNT), is_simulated=True, execution_decision_id="modern-81",
            occurred_at_ms=4000,
        )
        rows[3].payload = serialize_event(intent)
        rows[3].event_id = intent.event_id
        rows[3].schema_version = 3
    elif fault == "forged_flag":
        rows[3].payload["is_legacy_uncorrelated"] = True
    elif fault == "row_cid":
        rows[4].cid = 99
    elif fault == "row_venue":
        rows[4].venue_offer_id = "forged-column"
    elif fault == "payload_account":
        rows[4].payload["account_id"] = str(UUID(int=99))
    sqlite_session.add_all(rows)
    sentinel = OfferClaimRow(
        account_id=str(ACCOUNT), exchange_account_id=ACCOUNT, deployment_environment="prod",
        cid=900, symbol="fUST", state="claimed", venue_offer_id="runtime-sentinel",
        size_usdt=Decimal("123"), signal_correlation_id=str(SIGNAL),
        occurred_at_ms=1, last_updated_ms=1, last_event_seq=1,
    )
    sqlite_session.add(sentinel)
    await sqlite_session.commit()
    before_hash = canonical_event_hash(rows)
    with pytest.raises((ValueError, TypeError, RuntimeError)):
        await PostgresEventStore(deployment_environment="prod").rebuild_snapshot_from_log(
            sqlite_session, account_id=str(ACCOUNT), deployment_environment="prod", symbol=symbol,
        )
    # No rollback here: prevalidation itself must preserve the derived rows.
    claims = (await sqlite_session.scalars(select(OfferClaimRow))).all()
    assert [(row.cid, row.venue_offer_id, row.size_usdt) for row in claims] == [
        (900, "runtime-sentinel", Decimal("123")),
    ]
    assert canonical_event_hash(rows) == before_hash


@pytest.mark.parametrize("symbol", [None, "fUST"])
@pytest.mark.parametrize("amount", ["5", "7"])
async def test_failed_cycle_can_start_another_intent(sqlite_session: AsyncSession, symbol, amount) -> None:
    await _create_all(sqlite_session)
    rows = historical_rows(first_amount=amount)
    rows[1].event_type = "RESERVATION_FAILED"
    rows[1].venue_offer_id = rows[1].payload["venue_offer_id"] = None
    rows[1].payload["reason"] = "submit_failed"
    del rows[2]
    sqlite_session.add_all(rows)
    await sqlite_session.flush()
    await PostgresEventStore(deployment_environment="prod").rebuild_snapshot_from_log(
        sqlite_session, account_id=str(ACCOUNT), deployment_environment="prod", symbol=symbol,
    )
    claim = (await sqlite_session.scalars(select(OfferClaimRow))).one()
    position = (await sqlite_session.scalars(select(PositionStateRow))).one()
    assert (claim.state, claim.venue_offer_id) == ("released", "old-b")
    assert position.realized == Decimal("5")


@pytest.mark.parametrize("symbol", [None, "fUST"])
async def test_released_cycle_allows_new_correlation_and_amount(sqlite_session, symbol) -> None:
    await _create_all(sqlite_session)
    rows = historical_rows(first_amount="7")
    rows[2].event_type = "RESERVATION_RELEASED"
    rows[2].payload["reason"] = "venue_cancel"
    next_signal = str(UUID(int=82))
    for row in rows[3:]:
        row.payload["signal_correlation_id"] = next_signal
    sqlite_session.add_all(rows)
    await sqlite_session.flush()
    assert historical_claim_reset_sequences(rows, account_id=str(ACCOUNT), environment="prod") == frozenset({rows[3].event_seq})
    await PostgresEventStore(deployment_environment="prod").rebuild_snapshot_from_log(
        sqlite_session, account_id=str(ACCOUNT), deployment_environment="prod", symbol=symbol,
    )
    claim = (await sqlite_session.scalars(select(OfferClaimRow))).one()
    position = (await sqlite_session.scalars(select(PositionStateRow))).one()
    assert (claim.signal_correlation_id, claim.size_usdt) == (next_signal, 5)
    assert (position.reserved, position.realized) == (0, 5)


async def test_exact_historical_venue_duplicate_cannot_enter_replay_twice(sqlite_session) -> None:
    await _create_all(sqlite_session)
    rows = historical_rows()
    sqlite_session.add_all(rows)
    await sqlite_session.commit()
    before = canonical_event_hash(rows)
    with pytest.raises(IntegrityError):
        async with sqlite_session.begin_nested():
            sqlite_session.add(historical_rows()[2])
            await sqlite_session.flush()
    await PostgresEventStore(deployment_environment="prod").rebuild_snapshot_from_log(
        sqlite_session, account_id=str(ACCOUNT), deployment_environment="prod",
    )
    position = (await sqlite_session.scalars(select(PositionStateRow))).one()
    assert position.realized == 10
    stored = (await sqlite_session.scalars(select(EventLogRow).order_by(EventLogRow.event_seq))).all()
    assert len(stored) == 6
    assert canonical_event_hash(stored) == before


@pytest.mark.parametrize("fault", ["account", "environment", "order", "duplicate", "transient"])
async def test_policy_rejects_untrusted_scope_and_sequence(sqlite_session: AsyncSession, fault) -> None:
    await _create_all(sqlite_session)
    rows = historical_rows()
    if fault != "transient":
        sqlite_session.add_all(rows)
        await sqlite_session.flush()
    if fault == "order":
        rows.reverse()
    if fault == "duplicate":
        rows.append(rows[-1])
    with pytest.raises((TypeError, ValueError)):
        historical_claim_reset_sequences(
            rows, account_id=str(UUID(int=99)) if fault == "account" else str(ACCOUNT),
            environment="ci" if fault == "environment" else "prod",
        )


@pytest.mark.parametrize("symbol", [None, "fUST"])
async def test_rebuild_rejects_store_environment_mismatch(sqlite_session, symbol) -> None:
    await _create_all(sqlite_session)
    rows = historical_rows()
    sqlite_session.add_all(rows)
    await sqlite_session.flush()
    with pytest.raises(ValueError, match="environment"):
        await PostgresEventStore(deployment_environment="ci").rebuild_snapshot_from_log(
            sqlite_session, account_id=str(ACCOUNT), deployment_environment="prod", symbol=symbol,
        )
    assert (await sqlite_session.scalars(select(OfferClaimRow))).all() == []


async def test_policy_never_authorizes_historical_payload_with_audited_attempt(sqlite_session) -> None:
    await _create_all(sqlite_session)
    rows = historical_rows()
    rows[3].payload["submission_attempt"] = {"execution_decision_id": "audited-attempt"}
    sqlite_session.add_all(rows)
    await sqlite_session.flush()
    with pytest.raises(ValueError, match=r"historical|cycle"):
        historical_claim_reset_sequences(rows, account_id=str(ACCOUNT), environment="prod")


@pytest.mark.parametrize("key", ["execution_decision_id", "submission_attempt"])
async def test_policy_rejects_audit_identity_hidden_in_legacy_claim_payload(sqlite_session, key) -> None:
    await _create_all(sqlite_session)
    rows = historical_rows()
    # Legacy construction ignores fields not declared by ReservationClaimed.
    # Such a payload still must never receive a historical cycle exception.
    rows[4].payload[key] = "modern-audit-identity"
    sqlite_session.add_all(rows)
    await sqlite_session.flush()
    with pytest.raises(ValueError, match="historical"):
        historical_claim_reset_sequences(rows, account_id=str(ACCOUNT), environment="prod")


@pytest.mark.parametrize("symbol", [None, "fUST"])
@pytest.mark.parametrize("versioned", [False, True])
async def test_existing_stored_alias_upcast_has_identical_fill_contributions(
    sqlite_session, symbol, versioned,
) -> None:
    await _create_all(sqlite_session)
    rows = historical_rows(first_amount="7")
    for row in rows:
        del row.payload["amount"]
        del row.payload["symbol"]
        if versioned:
            row.payload.update(__schema_version__=2, __event_type__=row.event_type)
    sqlite_session.add_all(rows)
    await sqlite_session.flush()
    before = canonical_event_hash(rows)
    await PostgresEventStore(deployment_environment="prod").rebuild_snapshot_from_log(
        sqlite_session, account_id=str(ACCOUNT), deployment_environment="prod", symbol=symbol,
    )
    position = (await sqlite_session.scalars(select(PositionStateRow))).one()
    assert (position.symbol, position.reserved, position.realized) == ("fUST", 0, 12)
    assert canonical_event_hash(rows) == before


@pytest.mark.parametrize("symbol", [None, "fUST"])
async def test_reset_does_not_leak_to_strict_append(sqlite_session: AsyncSession, symbol) -> None:
    await _create_all(sqlite_session)
    rows = historical_rows()
    sqlite_session.add_all(rows)
    await sqlite_session.flush()
    store = PostgresEventStore(deployment_environment="prod")
    await store.rebuild_snapshot_from_log(
        sqlite_session, account_id=str(ACCOUNT), deployment_environment="prod", symbol=symbol,
    )
    await sqlite_session.commit()
    with pytest.raises(RuntimeError, match="claim identity conflict"):
        async with sqlite_session.begin_nested():
            await store.append(sqlite_session, ReservationClaimed(
                cid=81, venue_offer_id="live-conflict", symbol="fUST", amount=Decimal("5"),
                signal_correlation_id=SIGNAL, account_id=str(ACCOUNT), is_simulated=True,
                occurred_at_ms=9000, venue_seq=9,
                reservation_ref=ReservationRef(
                    execution_decision_id="live-decision", cid=81,
                    signal_correlation_id=SIGNAL, venue_offer_id="live-conflict",
                ),
            ))
    claim = (await sqlite_session.scalars(select(OfferClaimRow))).one()
    assert (claim.state, claim.venue_offer_id) == ("released", "old-b")
    assert len((await sqlite_session.scalars(select(EventLogRow))).all()) == 6


@pytest.mark.parametrize("symbol", [None, "fUST"])
async def test_modern_lifecycle_and_exact_duplicate_fill_retain_strict_semantics(
    sqlite_session: AsyncSession, symbol,
) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="prod")
    common = {"cid": 81, "symbol": "fUST", "amount": Decimal("5"),
              "signal_correlation_id": SIGNAL, "account_id": str(ACCOUNT), "is_simulated": True}
    await store.append(sqlite_session, ReservationIntent(
        **common, execution_decision_id="modern-81", occurred_at_ms=1000,
    ))
    ref = ReservationRef(execution_decision_id="modern-81", cid=81,
                         signal_correlation_id=SIGNAL, venue_offer_id="modern-venue")
    await store.append(sqlite_session, ReservationClaimed(
        **common, venue_offer_id="modern-venue", reservation_ref=ref,
        occurred_at_ms=2000, venue_seq=2,
    ))
    fill = OrderFilled(**common, venue_offer_id="modern-venue", reservation_ref=ref,
                       occurred_at_ms=3000, venue_seq=3, credit_id=None, fill_rate=0.001)
    await store.append(sqlite_session, fill)
    await store.append(sqlite_session, fill)
    rows = (await sqlite_session.scalars(select(EventLogRow).order_by(EventLogRow.event_seq))).all()
    assert len(rows) == 3
    assert historical_claim_reset_sequences(rows, account_id=str(ACCOUNT), environment="prod") == frozenset()
    await store.rebuild_snapshot_from_log(
        sqlite_session, account_id=str(ACCOUNT), deployment_environment="prod", symbol=symbol,
    )
    claim = (await sqlite_session.scalars(select(OfferClaimRow))).one()
    position = (await sqlite_session.scalars(select(PositionStateRow))).one()
    assert (claim.execution_decision_id, claim.state) == ("modern-81", "released")
    assert position.realized == Decimal("5")


@pytest.mark.parametrize("symbol", [None, "fUST"])
async def test_later_conflict_rolls_back_rebuild_and_other_transaction_writes(
    sqlite_session: AsyncSession, symbol,
) -> None:
    await _create_all(sqlite_session)
    rows = historical_rows()
    stale = historical_rows()[2]
    stale.venue_seq = stale.payload["venue_seq"] = 99
    rows.append(stale)
    sqlite_session.add_all(rows)
    await sqlite_session.commit()
    with pytest.raises(ValueError):
        async with sqlite_session.begin():
            sqlite_session.add(PositionStateRow(
                account_id=str(ACCOUNT), exchange_account_id=ACCOUNT,
                deployment_environment="prod", symbol="fUST", reserved=Decimal("99"),
                realized=Decimal("88"), last_updated_ms=1, last_event_seq=1,
            ))
            await PostgresEventStore(deployment_environment="prod").rebuild_snapshot_from_log(
                sqlite_session, account_id=str(ACCOUNT), deployment_environment="prod", symbol=symbol,
            )
    assert (await sqlite_session.scalars(select(PositionStateRow))).all() == []
    assert len((await sqlite_session.scalars(select(EventLogRow))).all()) == 7
