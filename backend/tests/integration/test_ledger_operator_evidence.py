"""S1-3c4a port and authoritative P3 checks on PostgreSQL; no live venue.

Invalid digest/coverage fixtures drop only the corresponding CHECK inside a
rolled-back transaction to exercise Python pre-checks independently of the DB
protections. Production constraints and migrations remain unchanged.
"""

from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.modules.execution.operator_evidence import LegacyOperatorEvidence
from bfx_funding_bot.modules.ledger import Quarantine, ResolutionRejected, ResolutionSubject, Scope
from bfx_funding_bot.modules.ledger.wiring import build_operator_evidence

from .test_ledger_journal import JOURNAL, OBSERVATION_ID, SCOPE, _engine, _resolution
from .test_ledger_schema_roles import (
    _observation_sql,
    ledger_db,  # noqa: F401 - seeded fixture dependency
)
from .test_ledger_schema_roles import (
    seeded as seeded_fixture,  # noqa: F401 - fixture re-export
)

pytestmark = pytest.mark.integration
PORT = build_operator_evidence()


async def _observation(
    session,
    *,
    accepted=True,
    complete=True,
    first="digest",
    confirmation="digest",
    started=20,
    history=True,
):
    query = await JOURNAL.begin_query(session, SCOPE, started)
    identifier = uuid4()
    statement = _observation_sql(
        str(identifier),
        str(query.query_id),
        accepted=accepted,
        complete=complete,
        first=first,
        confirmation=confirmation,
        accept_revision=query.start_revision,
        finished_at_ms=30,
    )
    if not history:
        changed = statement.replace(
            "      true, true, true, true,\n", "      true, true, false, true,\n"
        )
        assert changed != statement
        statement = changed
    await session.execute(text(statement))
    return identifier


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault,code",
    [
        ("not_latest", "stale_reconcile_fence"),
        ("not_accepted", "stale_reconcile_fence"),
        ("digest", "stale_reconcile_fence"),
        ("before_opening", "stale_reconcile_fence"),
        ("cross_scope", "stale_reconcile_fence"),
        ("history", "incomplete_offer_history_coverage"),
    ],
)
async def test_ledger_port_rejects_invalid_observation_pg(seeded_fixture, fault, code):  # noqa: F811
    engine = _engine(seeded_fixture)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            quarantine = Quarantine(uuid4(), "fUST", Decimal("1"), 10, {})
            await JOURNAL.open_quarantine(session, SCOPE, quarantine)
            if fault == "digest":
                await session.execute(
                    text(
                        "ALTER TABLE ledger_observation DROP CONSTRAINT ck_ledger_observation_matching_digest"
                    )
                )
            if fault == "history":
                await session.execute(
                    text(
                        "ALTER TABLE ledger_observation DROP CONSTRAINT ck_ledger_observation_acceptance"
                    )
                )
            identifier = await _observation(
                session,
                accepted=fault != "not_accepted",
                confirmation="different" if fault == "digest" else "digest",
                started=10 if fault == "before_opening" else 20,
                history=fault != "history",
            )
            if fault == "not_latest":
                await _observation(session)
            scope = Scope(SCOPE.exchange_account_id, "other") if fault == "cross_scope" else SCOPE
            with pytest.raises(ResolutionRejected, match=code):
                await PORT.verify(
                    session,
                    scope,
                    ResolutionSubject(quarantine.quarantine_id, "fUST"),
                    f"ledger:v1:obs:{identifier}",
                    require_history=True,
                )
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_p3_journal_rejects_nonlatest_or_unaccepted_observation(seeded_fixture, accepted):  # noqa: F811
    engine = _engine(seeded_fixture)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            quarantine = Quarantine(uuid4(), "fUST", Decimal("1"), 0, {})
            await JOURNAL.open_quarantine(session, SCOPE, quarantine)
            identifier = await _observation(session, accepted=accepted)
            if accepted:
                await _observation(session)
            with pytest.raises(ResolutionRejected, match="latest accepted"):
                await JOURNAL.record_resolution(
                    session,
                    SCOPE,
                    replace(
                        _resolution(
                            quarantine_id=quarantine.quarantine_id, observation_id=identifier
                        ),
                        resolved_at_ms=40,
                    ),
                )
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_evidence_namespaces_are_not_interchangeable_pg(seeded_fixture):  # noqa: F811
    engine = _engine(seeded_fixture)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            subject = ResolutionSubject(uuid4(), "fUST")
            with pytest.raises(ResolutionRejected, match="stale_reconcile_fence"):
                await PORT.verify(session, SCOPE, subject, "42", require_history=True)
            with pytest.raises(ResolutionRejected, match="stale_reconcile_fence"):
                await LegacyOperatorEvidence().verify(
                    session, SCOPE, subject, f"ledger:v1:obs:{OBSERVATION_ID}", require_history=True
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_latest_accepted_remains_evidence_after_a_newer_unaccepted_query(seeded_fixture):  # noqa: F811
    engine = _engine(seeded_fixture)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            quarantine = Quarantine(uuid4(), "fUST", Decimal("1"), 10, {})
            await JOURNAL.open_quarantine(session, SCOPE, quarantine)
            identifier = await _observation(session)
            await _observation(session, accepted=False)
            subject = ResolutionSubject(quarantine.quarantine_id, "fUST")
            verified = await PORT.verify(
                session, SCOPE, subject, f"ledger:v1:obs:{identifier}", require_history=True
            )
            assert verified.query_started_at_ms == 20
            await JOURNAL.record_resolution(
                session,
                SCOPE,
                replace(
                    _resolution(quarantine_id=quarantine.quarantine_id, observation_id=identifier),
                    resolved_at_ms=40,
                ),
            )
            await session.rollback()
    finally:
        await engine.dispose()
