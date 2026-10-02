"""The ledger operator read model and ``verify``, run as the web API's own role.

Every read below runs under ``SET LOCAL ROLE bfx_webapi``: a statement that touches
a column outside the migration's allowlist fails here, not only in production.

Mutation checks (one at a time; revert after each):

* ``verify`` back to ``session.get``: ``test_verify_runs_under_the_column_grant``.
* ``unresolved_quarantines`` without explicit columns: ``test_open_set_runs_under_the_column_grant``.
* R6 filtered out (``source_attempt_id IS NULL``): ``test_open_set_*``.
* A resolved attempt reported open: ``test_open_set_*`` and ``test_detail_*``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import (
    Quarantine,
    Resolution,
    ResolutionRejected,
    ResolutionSubject,
    Scope,
    UncertaintyView,
    observation_evidence_ref,
)
from bfx_funding_bot.modules.ledger._internal.provenance import offer_provenance
from bfx_funding_bot.modules.ledger.wiring import build_operator_evidence, build_operator_reads

from .test_ledger_capital_reader import JOURNAL, SCOPE, Book
from .test_ledger_capital_reader import book as book
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture dependency

pytestmark = pytest.mark.integration
READS = build_operator_reads()
EVIDENCE = build_operator_evidence()


@asynccontextmanager
async def _webapi(book: Book) -> AsyncIterator[AsyncSession]:
    async with book.factory() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE bfx_webapi"))
        assert await session.scalar(text("SELECT current_user")) == "bfx_webapi"
        yield session


async def _quarantine(book: Book, opened_at_ms: int, *, source: UUID | None = None) -> UUID:
    quarantine_id = uuid4()
    async with book.factory.begin() as session:
        await JOURNAL.open_quarantine(
            session,
            book.scope,
            Quarantine(
                quarantine_id, "fUST", Decimal("10"), opened_at_ms, {"secret": "evidence"},
                source_attempt_id=source,
            ),
        )
    return quarantine_id


async def _resolve(
    book: Book, resolved_at_ms: int, *, attempt: UUID | None = None,
    quarantine: UUID | None = None, actor: str = "operator", action: str = "not_accepted",
) -> None:
    assert book.observation_id is not None
    async with book.factory.begin() as session:
        await JOURNAL.record_resolution(
            session,
            book.scope,
            Resolution(
                uuid4(), "fUST", action,  # type: ignore[arg-type]
                None, book.observation_id, actor, "op-7", resolved_at_ms, "checked by hand",
                {"secret": "evidence"}, attempt_id=attempt, quarantine_id=quarantine,
            ),
        )


class Scenario:
    open_unknown: UUID
    resolved_unknown: UUID
    acked: UUID
    orphan: UUID
    r6: UUID
    resolved_quarantine: UUID


@pytest_asyncio.fixture
async def scenario(book: Book) -> Scenario:
    await book.accept()
    s = Scenario()
    s.open_unknown = await book.attempt("200", outcome="unknown")
    s.resolved_unknown = await book.attempt("100", outcome="unknown")
    s.acked = await book.attempt("30", outcome="ack", venue_offer_id="o-1")
    s.orphan = await _quarantine(book, 20)
    s.r6 = await _quarantine(book, 30, source=s.acked)
    s.resolved_quarantine = await _quarantine(book, 10)
    await book.accept(started=50, finished=51, confirmed=52)
    await _resolve(book, 100, attempt=s.resolved_unknown)
    await _resolve(book, 200, quarantine=s.resolved_quarantine, actor="system", action="manual")
    return s


async def _list(book: Book, **kwargs: Any) -> tuple[UncertaintyView, ...]:
    async with _webapi(book) as session:
        return await READS.list_uncertainties(
            session, book.scope, state=kwargs.get("state"), limit=kwargs.get("limit", 50)
        )


async def _get(book: Book, uncertainty_id: UUID, scope: Scope = SCOPE) -> UncertaintyView | None:
    async with _webapi(book) as session:
        return await READS.get_uncertainty(session, scope, uncertainty_id)


def _ids(views: tuple[UncertaintyView, ...]) -> list[UUID]:
    return [view.uncertainty_id for view in views]


@pytest.mark.asyncio
async def test_open_set_runs_under_the_column_grant(book, scenario) -> None:
    opened = await _list(book, state="open")
    # UNKNOWN attempts and quarantines (R6 included) without a resolution; never an
    # in-flight or merely acknowledged attempt, never a resolved subject.
    await book.attempt("50", outcome=None)  # in flight: not an uncertainty
    opened = await _list(book, state="open")
    assert set(_ids(opened)) == {scenario.open_unknown, scenario.orphan, scenario.r6}
    assert {view.state for view in opened} == {"open"}
    # Newest opening first: the R6 quarantine (30), the orphan (20), the attempt (0).
    assert _ids(opened) == [scenario.r6, scenario.orphan, scenario.open_unknown]
    stamps = [view.opened_at_ms for view in opened]
    assert stamps == sorted(stamps, reverse=True)


@pytest.mark.asyncio
async def test_resolved_set_is_the_journal_newest_first(book, scenario) -> None:
    resolved = await _list(book, state="resolved")
    assert _ids(resolved) == [scenario.resolved_quarantine, scenario.resolved_unknown]
    assert [view.resolved_at_ms for view in resolved] == [200, 100]
    assert {view.state for view in resolved} == {"resolved"}
    assert _ids(await _list(book, state="resolved", limit=1)) == [scenario.resolved_quarantine]


@pytest.mark.asyncio
async def test_without_a_state_open_comes_first_and_the_limit_cuts_the_tail(
    book, scenario,
) -> None:
    everything = await _list(book)
    assert [view.state for view in everything] == ["open"] * 3 + ["resolved"] * 2
    assert _ids(everything)[3:] == [scenario.resolved_quarantine, scenario.resolved_unknown]
    assert _ids(await _list(book, limit=4))[3:] == [scenario.resolved_quarantine]
    assert len(await _list(book, limit=2)) == 2
    assert all(view.state == "open" for view in await _list(book, limit=2))


@pytest.mark.asyncio
async def test_views_carry_the_mapped_fields(book, scenario) -> None:
    attempt = await _get(book, scenario.open_unknown)
    assert attempt == UncertaintyView(
        uncertainty_id=scenario.open_unknown, kind="submit_outcome_unknown", symbol="fUST",
        state="open", intended_amount=Decimal("200"),
        evidence={"outcome_reason": "test", "observed_at_ms": 0},
        attempt_id=scenario.open_unknown, opened_at_ms=0,
    )
    r6 = await _get(book, scenario.r6)
    assert r6 is not None
    assert (r6.kind, r6.intended_amount, r6.attempt_id) == (
        "unattributed_venue_offer", Decimal("10"), None,
    )
    assert dict(r6.evidence) == {"outcome_reason": "acked_offer_unobserved"}
    orphan = await _get(book, scenario.orphan)
    assert orphan is not None and dict(orphan.evidence) == {}
    # The quarantine's own evidence never reaches the view.
    assert "secret" not in repr(r6) + repr(orphan)
    operator = await _get(book, scenario.resolved_unknown)
    assert operator is not None
    assert (operator.state, operator.resolved_by_operator_id, operator.resolution_reason) == (
        "resolved", "op-7", "checked by hand",
    )
    assert operator.resolved_at_ms == 100
    system = await _get(book, scenario.resolved_quarantine)
    assert system is not None
    assert (system.state, system.resolved_by_operator_id, system.resolution_reason) == (
        "resolved", None, "checked by hand",
    )


@pytest.mark.asyncio
async def test_detail_returns_none_for_everything_that_is_not_an_uncertainty(
    book, scenario,
) -> None:
    in_flight = await book.attempt("50", outcome=None)
    for subject in (in_flight, scenario.acked, uuid4()):
        assert await _get(book, subject) is None
    other = Scope(uuid4(), "ci")
    for subject in (scenario.open_unknown, scenario.orphan, scenario.resolved_quarantine):
        assert await _get(book, subject, other) is None
        assert await _get(book, subject) is not None
    assert (await _get(book, scenario.resolved_unknown)).state == "resolved"  # type: ignore[union-attr]
    assert (await _get(book, scenario.open_unknown)).state == "open"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_verify_runs_under_the_column_grant(book, scenario) -> None:
    reference = observation_evidence_ref(book.observation_id)
    subject = ResolutionSubject(scenario.open_unknown, "fUST", scenario.open_unknown)
    async with _webapi(book) as session:
        verified = await EVIDENCE.verify(
            session, book.scope, subject, reference, require_history=True
        )
        assert verified.evidence_ref == reference
        quarantine = ResolutionSubject(scenario.orphan, "fUST", None)
        await EVIDENCE.verify(session, book.scope, quarantine, reference, require_history=True)
    async with _webapi(book) as session:
        with pytest.raises(ResolutionRejected) as wrong_symbol:
            await EVIDENCE.verify(
                session, book.scope, ResolutionSubject(scenario.open_unknown, "fUSD", scenario.open_unknown),
                reference, require_history=True,
            )
        assert wrong_symbol.value.kind == "not_found"
    async with _webapi(book) as session:
        with pytest.raises(ResolutionRejected, match="stale_reconcile_fence"):
            await EVIDENCE.verify(
                session, book.scope, subject, observation_evidence_ref(uuid4()),
                require_history=True,
            )


@pytest.mark.asyncio
async def test_a_newer_acceptance_makes_the_older_reference_stale(book, scenario) -> None:
    older = observation_evidence_ref(book.observation_id)
    await book.accept(started=60, finished=61, confirmed=62)
    assert observation_evidence_ref(book.observation_id) != older
    subject = ResolutionSubject(scenario.open_unknown, "fUST", scenario.open_unknown)
    async with _webapi(book) as session:
        with pytest.raises(ResolutionRejected, match="stale_reconcile_fence"):
            await EVIDENCE.verify(session, book.scope, subject, older, require_history=True)
    async with _webapi(book) as session:
        await EVIDENCE.verify(
            session, book.scope, subject, observation_evidence_ref(book.observation_id),
            require_history=True,
        )


@pytest.mark.asyncio
async def test_offer_provenance_names_only_allowlisted_columns(book, scenario) -> None:
    async with _webapi(book) as session:
        assert await offer_provenance(session, book.scope, ["o-1", "o-2"]) == {
            "o-1": {scenario.acked}, "o-2": set(),
        }


@pytest.mark.asyncio
async def test_resolved_list_plan_uses_the_scope_index(book, scenario) -> None:
    """The real resolved statement walks ``ix_execution_resolution_scope_resolved``, unsorted."""
    engine = book.factory.kw["bind"]
    captured: list[tuple[str, Any]] = []

    def grab(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        if "ORDER BY execution_resolution_journal.resolved_at_ms DESC" in statement:
            captured.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", grab)
    try:
        await _list(book, state="resolved", limit=5)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", grab)
    assert len(captured) == 1
    statement, parameters = captured[0]
    async with engine.connect() as conn:
        await conn.exec_driver_sql("SET enable_seqscan = off")
        rows = await conn.exec_driver_sql("EXPLAIN " + statement, parameters)
        plan = "\n".join(line for (line,) in rows.all())
    assert "ix_execution_resolution_scope_resolved" in plan, plan
    assert "Sort" not in plan, plan


@pytest.mark.asyncio
async def test_the_router_serves_the_ledger_read_model_as_the_web_api_role(
    book, scenario, monkeypatch,
) -> None:
    """End to end: HTTP list and detail over the ledger, every statement as ``bfx_webapi``."""
    import httpx
    from fastapi import FastAPI

    from bfx_funding_bot.apps.read_models import select_read_models
    from bfx_funding_bot.core.auth import Principal, require_operator
    from bfx_funding_bot.modules.accounts.exchange_accounts import grant_membership
    from bfx_funding_bot.modules.api.deps import get_session
    from bfx_funding_bot.modules.api.uncertainties import build_uncertainties_router

    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    account = SCOPE.exchange_account_id
    async with book.factory.begin() as session:
        await grant_membership(
            session, exchange_account_id=account, user_id="operator-1", role="owner"
        )

    async def restricted() -> AsyncIterator[AsyncSession]:
        async with _webapi(book) as session:
            yield session

    app = FastAPI()
    app.include_router(build_uncertainties_router())
    app.state.read_models = select_read_models("ledger")
    app.dependency_overrides[require_operator] = lambda: Principal("operator-1", None, "admin")
    app.dependency_overrides[get_session] = restricted
    base = f"/api/v1/exchange-accounts/{account}/uncertainties"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get(base, params={"state": "open"})
        assert listed.status_code == 200, listed.text
        rows = {row["uncertaintyId"]: row for row in listed.json()["data"]}
        assert set(rows) == {str(scenario.open_unknown), str(scenario.orphan), str(scenario.r6)}
        attempt = rows[str(scenario.open_unknown)]
        assert attempt["kind"] == "submit_outcome_unknown"
        assert attempt["intendedAmount"] == "200"
        assert attempt["evidenceSummary"] == {"outcomeReason": "test", "observedAtMs": 0}
        assert attempt["resolutionContext"]["unavailableReason"] == "matcher_pending"
        assert rows[str(scenario.r6)]["evidenceSummary"] == {
            "outcomeReason": "acked_offer_unobserved"
        }
        resolved = await client.get(f"{base}/{scenario.resolved_unknown}")
        assert resolved.status_code == 200, resolved.text
        data = resolved.json()["data"]
        assert (data["state"], data["resolvedByOperatorId"], data["resolutionReason"]) == (
            "resolved", "op-7", "checked by hand",
        )
        in_flight = await book.attempt("50", outcome=None)
        missing = await client.get(f"{base}/{in_flight}")
        assert (missing.status_code, missing.json()["detail"]) == (404, "not_found")
