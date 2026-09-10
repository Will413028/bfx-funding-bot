"""Real temporary projector sees captured uncommitted rows, never commits source."""

from decimal import Decimal

import pytest
from sqlalchemy import select, text, update

from bfx_funding_bot.modules.execution.event_store.entities import (
    VenueCreditObservation,
    VenueOfferObservation,
)
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, ProjectionHeadRow
from bfx_funding_bot.modules.execution.events import SnapshotCoverage, VenueSnapshotObserved
from scripts.verify_projection_replay import replay_one_account
from tests.integration.test_projection_cutover_diagnostics import _seed, _source_state
from tests.modules.execution.event_store.test_historical_claim_cycles import ACCOUNT
from tests.modules.execution.event_store.test_replay_verification import runtime

pytestmark = pytest.mark.integration


async def _sequences(factory):
    async with factory() as session:
        names = (await session.scalars(text(
            "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' "
            "ORDER BY sequencename"
        ))).all()
        return {
            name: (await session.execute(text(
                f'SELECT last_value, is_called FROM public."{name}"'
            ))).one()
            for name in names
        }


async def _captured(session):
    return list(await session.scalars(select(EventLogRow).where(
        EventLogRow.exchange_account_id == ACCOUNT,
        EventLogRow.deployment_environment == "ci",
    ).order_by(EventLogRow.event_seq)))


async def test_uncommitted_snapshot_replays_without_public_writes_or_source_commit(pg_session_factory):
    kernel = runtime()
    await _seed(pg_session_factory)
    # The diagnostic fixture deliberately corrupts the cursor. A real append
    # needs the original valid cursor, not a replay of already-completed claims.
    async with pg_session_factory() as setup:
        await setup.execute(update(ProjectionHeadRow).values(last_event_seq=6))
        await setup.commit()
    original = await _source_state(pg_session_factory)
    async with pg_session_factory() as session:
        assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"
        before = await kernel.replay_captured_rows(
            session, rows=await _captured(session), account_id=ACCOUNT, environment="ci",
        )
        snapshot = VenueSnapshotObserved(
            account_id=str(ACCOUNT), environment="ci",
            query_started_at_ms=10000, query_finished_at_ms=10001,
            offers=(VenueOfferObservation(
                venue_offer_id="captured-only", symbol="fUST",
                amount_original=Decimal("7"), amount_remaining=Decimal("7"),
                rate=Decimal("0.01"), period_days=2, status="active",
                mts_created=10000, mts_updated=10001,
            ),),
            credits=(VenueCreditObservation(
                credit_id="captured-credit", symbol="fUST", amount=Decimal("11"),
                rate=Decimal("0.01"), period_days=2, status="active",
            ),),
            wallet_available={"fUST": Decimal("3")},
            coverage=SnapshotCoverage(True, True, True),
        )
        await PostgresEventStore(deployment_environment="ci").append_snapshot(session, snapshot)
        rows = await _captured(session)
        assert len(rows) == 7 and rows[-1].event_id == snapshot.event_id
        transaction = session.get_transaction()
        own_rows = {name: (await session.scalars(text(
            f"SELECT to_jsonb(t) FROM public.{name} t ORDER BY to_jsonb(t)::text"
        ))).all() for name in original}
        sequences = await _sequences(pg_session_factory)  # After caller's legitimate append.
        async with pg_session_factory() as observer:
            assert len(await _captured(observer)) == 6
        report = await kernel.replay_captured_rows(
            session, rows=rows, account_id=ACCOUNT, environment="ci",
        )
        assert report.event_head == rows[-1].event_seq > before.event_head
        assert report.event_hash != before.event_hash
        assert report.row_counts["event_log"] == 7
        assert report.row_counts["venue_offer_state"] == 1
        assert report.row_counts["venue_credit_state"] == 1
        assert report.replayed_offer_exposure_by_symbol == {"fUST": Decimal("7")}
        assert report.replayed_credit_exposure_by_symbol == {"fUST": Decimal("11")}
        assert report.diagnostic_old_row_counts is None
        assert session.get_transaction() is transaction and transaction.is_active
        assert await _source_state(pg_session_factory) == original
        assert await _sequences(pg_session_factory) == sequences
        assert {name: (await session.scalars(text(
            f"SELECT to_jsonb(t) FROM public.{name} t ORDER BY to_jsonb(t)::text"
        ))).all() for name in original} == own_rows
        await session.rollback()
    assert await _source_state(pg_session_factory) == original


async def test_operator_and_captured_historical_replay_have_identical_evidence(pg_session_factory):
    kernel = runtime()
    await _seed(pg_session_factory)
    async with pg_session_factory() as session:
        operator = await replay_one_account(
            session, account_id=ACCOUNT, environment="ci", projector_version="execution-state-v1",
        )
        report = await kernel.replay_captured_rows(
            session, rows=await _captured(session), account_id=ACCOUNT, environment="ci",
            expected_event_hash=operator.event_hash,
        )
        assert report.row_counts == operator.row_counts
        assert report.content_hashes == operator.content_hashes
        assert report.event_ids == operator.event_ids
        assert report.event_head == operator.event_head == 6
        assert report.replayed_offer_exposure_by_symbol == operator.replayed_offer_exposure_by_symbol
        assert report.replayed_credit_exposure_by_symbol == operator.replayed_credit_exposure_by_symbol
        assert report.row_counts["offer_claims"] == report.row_counts["position_state"] == 1
        # Captured by running the reviewed 0caaaf9 operator implementation on
        # this synthetic fixture in disposable PG, independently of this kernel.
        empty_hash = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
        assert report.content_hashes == {
            "event_log": "3b4d77f9ee7647971c1b4a78f37cc92ea31b0ce63f0225b49e0f3c3cbfefd349",
            "offer_claims": "186c2458cf9b7fde4497704d8dceb89449c91b6f1bd987633e77797de4061c2c",
            "position_state": "27fad5bb91485b3f4b952d82267843bf769d344c54411fb98a557268639774a0",
            "projection_heads": "2ddb0853c39af8319146f4905f351a94789cbb4787c01c08731636be9b951ed5",
            "venue_offer_state": empty_hash, "venue_credit_state": empty_hash,
            "reconcile_observation": empty_hash, "submission_attempts": empty_hash,
            "execution_uncertainties": empty_hash,
        }


async def test_empty_captured_stream_replays_empty_even_with_public_events(pg_session_factory):
    kernel = runtime()
    await _seed(pg_session_factory)
    async with pg_session_factory() as session:
        report = await kernel.replay_captured_rows(
            session, rows=[], account_id=ACCOUNT, environment="ci",
        )
        assert report.event_head is None and report.event_ids == ()
        assert report.row_counts == {
            "event_log": 0, "offer_claims": 0, "position_state": 0,
            "venue_offer_state": 0, "venue_credit_state": 0, "projection_heads": 1,
            "reconcile_observation": 0, "submission_attempts": 0, "execution_uncertainties": 0,
        }
        assert report.replayed_offer_exposure_by_symbol == {}
        assert report.replayed_credit_exposure_by_symbol == {}


@pytest.mark.parametrize("isolation,allowed", [
    ("READ COMMITTED", False), ("REPEATABLE READ", True), ("SERIALIZABLE", True),
])
async def test_collector_requires_source_snapshot(pg_session_factory, isolation, allowed):
    kernel = runtime()
    await _seed(pg_session_factory)
    async with pg_session_factory() as session:
        await session.connection(execution_options={"isolation_level": isolation})
        rows = await _captured(session)
        raw = {}

        def collect(table, before, after, *, key_columns):
            raw[table] = (before, after)

        if not allowed:
            with pytest.raises(kernel.ReplayVerificationError, match="snapshot"):
                await kernel.replay_captured_rows(
                    session, rows=rows, account_id=ACCOUNT, environment="ci",
                    diagnostic_collector=collect,
                )
            assert raw == {}
            return
        report = await kernel.replay_captured_rows(
            session, rows=rows, account_id=ACCOUNT, environment="ci", diagnostic_collector=collect,
        )
        assert len(raw) == 8
        assert raw["offer_claims"][0][0]["last_updated_ms"] == 999
        assert raw["offer_claims"][1][0]["last_updated_ms"] == 6000
        assert raw["projection_heads"][1][0]["last_event_seq"] == report.event_head == 6
        assert "updated_at" in raw["projection_heads"][1][0]


async def test_invalid_historical_claim_cycle_fails_without_source_writes(pg_session_factory):
    kernel = runtime()
    await _seed(pg_session_factory)
    original = await _source_state(pg_session_factory)
    sequences = await _sequences(pg_session_factory)
    async with pg_session_factory() as session:
        rows = await _captured(session)
        # An uncompleted old claim may not be silently replaced by the next
        # historical lifecycle, even though each individual event can decode.
        del rows[2]
        with pytest.raises(ValueError, match="historical"):
            await kernel.replay_captured_rows(
                session, rows=rows, account_id=ACCOUNT, environment="ci",
            )
        assert session.in_transaction()
        await session.execute(text("SELECT 1"))  # Source transaction is still usable.
    assert await _source_state(pg_session_factory) == original
    assert await _sequences(pg_session_factory) == sequences
