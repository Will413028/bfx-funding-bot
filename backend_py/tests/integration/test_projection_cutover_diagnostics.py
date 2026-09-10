"""Real PostgreSQL source snapshot and temporary projection field evidence."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select, text, update

from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    OfferClaimRow,
    PositionStateRow,
    ProjectionHeadRow,
)
from bfx_funding_bot.modules.execution.projection_cutover.codec import row_digest
from bfx_funding_bot.modules.execution.projection_cutover.diagnostics import compare_rows
from scripts.verify_projection_replay import (
    _TEMPORARY_TABLE_NAMES,
    ReplayVerificationError,
    render_replay_report,
    replay_one_account,
)
from tests.modules.execution.event_store.test_historical_claim_cycles import (
    ACCOUNT,
    historical_rows,
)

pytestmark = pytest.mark.integration
OTHER_ACCOUNT = UUID(int=987)


async def _seed(factory):
    async with factory() as session:
        session.add_all(
            [
                ExchangeAccount(id=ACCOUNT, venue="bitfinex", label="codec"),
                ExchangeAccount(id=OTHER_ACCOUNT, venue="bitfinex", label="other"),
            ]
        )
        session.add_all(historical_rows(environment="ci"))
        await session.flush()
        await PostgresEventStore(deployment_environment="ci").rebuild_snapshot_from_log(
            session, account_id=str(ACCOUNT), deployment_environment="ci"
        )
        await session.execute(update(OfferClaimRow).values(last_updated_ms=999))
        await session.execute(
            update(ProjectionHeadRow).values(
                updated_at=datetime(2000, 1, 1, tzinfo=UTC), last_event_seq=0
            )
        )
        for account, environment, symbol in (
            (ACCOUNT, "ci", "fUSD"),
            (OTHER_ACCOUNT, "ci", "fEUR"),
            (ACCOUNT, "other", "fGBP"),
        ):
            session.add(
                PositionStateRow(
                    account_id=str(account),
                    exchange_account_id=account,
                    deployment_environment=environment,
                    symbol=symbol,
                    reserved=Decimal("3.00"),
                    realized=Decimal("9.00"),
                    last_updated_ms=111,
                    last_event_seq=0,
                )
            )
        await session.commit()


async def _source_state(factory):
    async with factory() as session:
        return {
            name: (
                await session.execute(
                    text(f"SELECT to_jsonb(t) FROM public.{name} t ORDER BY to_jsonb(t)::text")
                )
            )
            .scalars()
            .all()
            for name in _TEMPORARY_TABLE_NAMES
        }


@pytest.mark.parametrize("existing_snapshot", [False, True])
async def test_temporary_diagnostics_preserve_source_scope_and_canonical_report(
    pg_session_factory,
    existing_snapshot,
):
    await _seed(pg_session_factory)
    async with pg_session_factory() as session:
        if existing_snapshot:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            await session.execute(select(OfferClaimRow))  # Establish MVCC snapshot.
            async with pg_session_factory() as writer:
                await writer.execute(update(OfferClaimRow).values(last_updated_ms=1000))
                await writer.commit()
        unchanged = await _source_state(pg_session_factory)
        changes, tables, raw = [], [], {}

        def collect(table, before, after, *, key_columns):
            tables.append(table)
            assert all(
                row["exchange_account_id"] == ACCOUNT and row["deployment_environment"] == "ci"
                for row in [*before, *after]
            )
            raw[table] = (before, after)
            changes.extend(compare_rows(table, before, after, key_columns=key_columns))

        report = await replay_one_account(
            session,
            account_id=ACCOUNT,
            environment="ci",
            projector_version="execution-state-v1",
            diagnostic_collector=collect,
        )
        plain = await replay_one_account(
            session, account_id=ACCOUNT, environment="ci", projector_version="execution-state-v1"
        )
        assert render_replay_report(report) == render_replay_report(plain)
    # Captured temporary rows remain usable after the connection has closed.
    assert len(tables) == 8 and len(set(tables)) == 8
    assert raw["offer_claims"][0][0]["last_updated_ms"] == 999
    assert raw["offer_claims"][1][0]["last_updated_ms"] == 6000
    assert {row["symbol"] for row in raw["position_state"][0]} == {"fUST", "fUSD"}
    assert {row["symbol"] for row in raw["position_state"][1]} == {"fUST"}
    assert ("offer_claims", "last_updated_ms") in {(d.table, d.column) for d in changes}
    assert ("projection_heads", "updated_at") in {(d.table, d.column) for d in changes}
    assert ("projection_heads", "last_event_seq") in {(d.table, d.column) for d in changes}
    missing_key = row_digest(
        {"exchange_account_id": ACCOUNT, "deployment_environment": "ci", "symbol": "fUSD"}
    )
    assert any(d.key_digest == missing_key and d.after_digest is None for d in changes)
    assert all(d.classification == "unexplained" for d in changes)
    assert await _source_state(pg_session_factory) == unchanged


async def test_diagnostics_reject_existing_read_committed_transaction(pg_session_factory):
    async with pg_session_factory() as session:
        await session.execute(text("SELECT 1"))
        with pytest.raises(ReplayVerificationError, match="snapshot"):
            await replay_one_account(
                session,
                account_id=ACCOUNT,
                environment="ci",
                projector_version="execution-state-v1",
                diagnostic_collector=lambda *a, **k: None,
            )
