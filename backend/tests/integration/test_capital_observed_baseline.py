"""F3 (i'): the read-only legacy entry answers exactly what acceptance + the read would, unwritten.

For each fixture the same observation pair is evaluated twice on the same database state:
first read-only (``evaluate_observation_read_only`` + ``read_observed``) with every SQL statement
captured, then live (``begin_snapshot`` + ``accept_snapshot`` + ``read_capital``). Field by field
the answers match (classification, recorded block, policy, snapshot, budget, unattributed
exposure), and so do refusals; the read-only path issues no write and takes no lock.

Mutations (apply one, run this file, revert):

* the read-only entry writes (e.g. ``evaluate_observation_read_only`` calls ``begin_snapshot``
  for its fence): ``test_read_only_entry_equals_live_acceptance`` fails on the
  captured statements;
* ``evaluate_scope`` / ``compare_ledger_arm`` without ``require_read_only_snapshot``:
  ``test_the_arm_refuses_a_read_write_transaction``.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import text

from bfx_funding_bot.apps.capital_comparison_ledger import compare_ledger_arm
from bfx_funding_bot.modules.execution.capital_observed_baseline import (
    CutoverObservation,
    LedgerObservationRef,
    evaluate_scope,
)
from bfx_funding_bot.modules.execution.capital_repository import (
    AppliedCapitalPolicy,
    CapitalBlockedError,
    CapitalRepository,
    policy_from_row,
    read_policy_row,
)
from bfx_funding_bot.modules.execution.capital_tables import CapitalSnapshotRow
from bfx_funding_bot.modules.execution.event_store.entities import (
    VenueCreditObservation,
    VenueOfferObservation,
)
from bfx_funding_bot.modules.execution.events import (
    ReservationUnknown,
    SnapshotCoverage,
    VenueSnapshotObserved,
)
from bfx_funding_bot.modules.ledger.wiring import build_ledger_capital_reader
from bfx_funding_bot.modules.trading import CapitalScope
from tests.integration.test_capital_repository import (
    _credit,
    _place,
    _sync_trade,
    authorize,
    capital_db,  # noqa: F401 - fixture
    repository,
    setup_policy,
    snapshot,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CELLS = ("a30", "p2")
_WRITES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "MERGE")


@pytest.fixture
def capital_engine(pg_engine):  # type: ignore[no-untyped-def]
    """PostgreSQL only: the arm's transaction checks read PostgreSQL settings."""
    return pg_engine


def observation(account: Any, available: str = "1000", offers: tuple[Any, ...] = (),
                credits: tuple[Any, ...] = (), history: tuple[Any, ...] = ()) -> tuple[VenueSnapshotObserved, VenueSnapshotObserved]:
    """The ``snapshot`` helper's observation pair (query 1000-1050, confirmation 1050-1060)."""
    event = VenueSnapshotObserved(
        account_id=str(account), environment="ci", query_started_at_ms=1000,
        query_finished_at_ms=1050, offers=offers, credits=credits, offer_history=history,
        wallet_available={"fUST": Decimal(available), "fUSD": Decimal("0")},
        coverage=SnapshotCoverage(True, True, True),
    )
    return event, replace(event, query_started_at_ms=1050, query_finished_at_ms=1060,
                          event_id=uuid4(), offer_history=())


type Answer = dict[str, object] | str


def _answer(view: Any) -> dict[str, object]:
    return {
        "applied": (view.applied.revision, view.applied.digest, view.applied.revision_id,
                    view.applied.policy),
        "snapshot": view.snapshot, "budget": view.budget,
        "unattributed": view.unattributed_credit_exposure,
        "attribution": dict(view.attribution),
    }


async def read_only(factory: Any, repo: CapitalRepository, pair: tuple[Any, Any], *,
                    accept_now: int, read_now: int) -> tuple[Answer, dict[str, Answer], list[str]]:
    engine = factory.kw["bind"].sync_engine
    statements: list[str] = []

    def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    sa_event.listen(engine, "before_cursor_execute", capture)
    try:
        async with factory() as session, session.begin():
            try:
                acceptance = await repo.evaluate_observation_read_only(
                    session, event=pair[0], confirmation=pair[1], now_ms=accept_now)
            except CapitalBlockedError as exc:
                return str(exc), {}, statements
            accepted: Answer = {"classification": acceptance.classification,
                                "blocked": acceptance.blocked_reason}
            cells: dict[str, Answer] = {}
            for cell in CELLS:
                row = await read_policy_row(session, account_id=repo.account_id,
                                            environment="ci", symbol="fUST")
                applied = AppliedCapitalPolicy(row.revision, row.digest, policy_from_row(row), row.id)
                try:
                    view = await repo.read_observed(session, acceptance, symbol="fUST", cell_id=cell,
                                                    now_ms=read_now, applied=applied)
                    cells[cell] = _answer(view)
                except CapitalBlockedError as exc:
                    cells[cell] = str(exc)
            await session.rollback()
    finally:
        sa_event.remove(engine, "before_cursor_execute", capture)
    return accepted, cells, statements


async def live(factory: Any, repo: CapitalRepository, pair: tuple[Any, Any], *,
               accept_now: int, read_now: int) -> tuple[Answer, dict[str, Answer]]:
    try:
        async with factory.begin() as session:
            fence = await repo.begin_snapshot(session, now_ms=pair[0].query_started_at_ms)
        async with factory.begin() as session:
            stored = await repo.accept_snapshot(session, fence=fence, event=pair[0],
                                                confirmation=pair[1], now_ms=accept_now)
    except CapitalBlockedError as exc:
        return str(exc), {}
    cells: dict[str, Answer] = {}
    async with factory.begin() as session:
        row = await session.get(CapitalSnapshotRow, stored.event_seq)
        assert row is not None
        accepted: Answer = {"classification": row.classification,
                            "blocked": row.authorization_blocked_reason}
        for cell in CELLS:
            try:
                cells[cell] = _answer(await repo.read_capital(session, symbol="fUST", cell_id=cell,
                                                              now_ms=read_now))
            except CapitalBlockedError as exc:
                cells[cell] = str(exc)
    return accepted, cells


async def _plain(factory: Any, repo: CapitalRepository) -> tuple[Any, Any]:
    await snapshot(factory, repo)
    return observation(repo.account_id, "900")


async def _partial_fill(factory: Any, repo: CapitalRepository) -> tuple[Any, Any]:
    seq = await snapshot(factory, repo)
    offer = await _place(factory, repo, _POLICY[repo.account_id], seq, amount="300", cid=1,
                         venue_offer_id="101")
    await snapshot(factory, repo, "700", offers=(offer,))
    filled = replace(offer, amount_remaining=Decimal("100"), status="partially_filled")
    return observation(repo.account_id, "700", offers=(filled,), credits=(_credit("c1", "200"),))


async def _funding_trade(factory: Any, repo: CapitalRepository) -> tuple[Any, Any]:
    seq = await snapshot(factory, repo)
    offer = await _place(factory, repo, _POLICY[repo.account_id], seq, amount="200", cid=1,
                         venue_offer_id="101")
    await snapshot(factory, repo, "800", offers=(offer,))
    await _sync_trade(factory, repo, trade_id=1, offer_id=101, amount="200")
    return observation(repo.account_id, "800", credits=(_credit("c1", "200"),))


async def _loan_split(factory: Any, repo: CapitalRepository) -> tuple[Any, Any]:
    seq = await snapshot(factory, repo)
    offer = await _place(factory, repo, _POLICY[repo.account_id], seq, amount="300", cid=1,
                         venue_offer_id="101")
    await snapshot(factory, repo, "700", offers=(offer,))

    def lent(credit_id: str, amount: str, created: int) -> VenueCreditObservation:
        return VenueCreditObservation(credit_id, "fUST", Decimal(amount), Decimal("0.0001"), 2,
                                      "active", mts_created=created, mts_opening=1150)

    await snapshot(factory, repo, "700", credits=(lent("loan:9", "300", 1150),))
    return observation(repo.account_id, "700",
                       credits=(lent("c1", "120", 1160), lent("c2", "180", 1170)))


async def _foreign(factory: Any, repo: CapitalRepository) -> tuple[Any, Any]:
    await snapshot(factory, repo)
    manual = VenueOfferObservation("manual-1", "fUST", Decimal("300"), Decimal("300"),
                                   Decimal("0.0001"), 2, "active", 1000, 1000)
    return observation(repo.account_id, "700", offers=(manual,))


async def _open_unknown(factory: Any, repo: CapitalRepository) -> tuple[Any, Any]:
    seq = await snapshot(factory, repo)
    result = await authorize(factory, repo, _POLICY[repo.account_id], seq)
    async with factory.begin() as session:
        await repo.writer.append(session, ReservationUnknown(
            symbol="fUST", cid=1, account_id=str(repo.account_id), is_simulated=True,
            signal_correlation_id=result.intent.signal_correlation_id,
            reservation_ref=result.intent.reservation_ref, amount=Decimal("200"),
            reason="test-outcome", occurred_at_ms=1150))
    return observation(repo.account_id, "800")


async def _unstable(factory: Any, repo: CapitalRepository) -> tuple[Any, Any]:
    await snapshot(factory, repo)
    event, confirmation = observation(repo.account_id, "900")
    return event, replace(confirmation, wallet_available={"fUST": Decimal("901"), "fUSD": Decimal(0)})


async def _inflight(factory: Any, repo: CapitalRepository) -> tuple[Any, Any]:
    seq = await snapshot(factory, repo)
    await authorize(factory, repo, _POLICY[repo.account_id], seq)  # no outcome yet
    return observation(repo.account_id, "800")


_POLICY: dict[Any, Any] = {}
FIXTURES: dict[str, tuple[Callable[[Any, CapitalRepository], Awaitable[tuple[Any, Any]]], int]] = {
    # name: (state + observation, accept clock); reads at 1100 like the repository tests.
    "plain": (_plain, 1060),
    "partial_fill_recent_fill": (_partial_fill, 1060),
    "funding_trade": (_funding_trade, 1060),
    "loan_split_carried": (_loan_split, 1060),
    "foreign_offer": (_foreign, 1060),
    "open_unknown": (_open_unknown, 1060),
    "unstable": (_unstable, 1060),
    "stale": (_plain, 1000 + 10_001),
    "inflight_command": (_inflight, 1060),
}
REFUSED = {"unstable": "snapshot_unstable", "stale": "snapshot_stale",
           "inflight_command": "snapshot_inflight_command"}


@pytest.mark.parametrize("name", sorted(FIXTURES))
async def test_read_only_entry_equals_live_acceptance(capital_db: Any, name: str) -> None:  # noqa: F811
    factory, account = capital_db
    repo = repository(account)
    _POLICY[account] = await setup_policy(factory, repo, "0", "0.70")
    build, accept_now = FIXTURES[name]
    pair = await build(factory, repo)

    ro_accepted, ro_cells, statements = await read_only(
        factory, repo, pair, accept_now=accept_now, read_now=1100)
    assert statements, "nothing was read"
    written = [s for s in statements
               if s.lstrip().upper().startswith(_WRITES) or "pg_advisory" in s.lower()]
    assert written == [], written

    live_accepted, live_cells = await live(factory, repo, pair, accept_now=accept_now, read_now=1100)
    assert ro_accepted == live_accepted
    assert ro_cells == live_cells
    if name in REFUSED:
        assert ro_accepted == REFUSED[name]
    elif name == "open_unknown":
        assert ro_cells == dict.fromkeys(CELLS, "execution_unknown")
    else:
        assert all(isinstance(answer, dict) for answer in ro_cells.values()), ro_cells
    if name == "partial_fill_recent_fill":
        assert isinstance(ro_accepted, dict)
        assert ro_accepted["classification"]["credit_cells"]["c1"]["basis"] == "recent_fill"


async def test_the_arm_refuses_a_read_write_transaction(capital_db: Any) -> None:  # noqa: F811
    """The legacy arm and the ledger arm only run in REPEATABLE READ READ ONLY."""
    factory, account = capital_db
    repo = repository(account)
    _POLICY[account] = await setup_policy(factory, repo, "0", "0.70")
    event, confirmation = await _plain(factory, repo)
    observed = CutoverObservation(account, "ci", event, confirmation,
                                  LedgerObservationRef(uuid4(), uuid4(), "0", "0"))
    async with factory() as session, session.begin():
        with pytest.raises(ValueError, match="repeatable_read_read_only_required"):
            await evaluate_scope(session, observed, now_ms=1060, max_snapshot_age_ms=10_000)
    async with factory() as session, session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        evaluated = await evaluate_scope(session, observed, now_ms=1060, max_snapshot_age_ms=10_000)
    async with factory() as session, session.begin():
        result = await compare_ledger_arm(
            session, scope=CapitalScope(account, "ci", "fUST", "a30"), evaluated=evaluated,
            ledger_reader=build_ledger_capital_reader(), now_ms=1060, max_snapshot_age_ms=10_000)
    assert (result.status, result.reason) == ("error", "ValueError")
