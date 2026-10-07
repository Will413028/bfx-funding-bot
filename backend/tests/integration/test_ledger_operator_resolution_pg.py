"""S1-3e4: the operator uncertainty-resolution request path under the ledger authority (PostgreSQL).

The web API validates and queues a request that cites a ledger observation; the account
daemon's worker re-validates it, matches the attempt against that observation and writes
the resolution journal row the request outcome then points at. Nothing touches the event
log. Mutation map (spec "Acceptance + mutations", numbers in the test docstrings).
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from bfx_funding_bot.modules.execution.operator_requests import FAILED, REJECTED
from bfx_funding_bot.modules.execution.uncertainty_requests import (
    ResolutionRejected as RequestRefused,
)
from bfx_funding_bot.modules.execution.uncertainty_requests import (
    ResolutionRequestPending,
    ResolutionScope,
    UncertaintyResolutionRequests,
    UncertaintyResolutionWorker,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import UncertaintyResolutionRequestRow
from bfx_funding_bot.modules.ledger import (
    MANUAL_RESOLUTION_DECISIONS,
    OfferHistory,
    Quarantine,
    QueuedResolution,
    Resolution,
    ResolutionIntent,
    ResolutionRejected,
    ResolutionSubject,
    observation_evidence_ref,
)
from bfx_funding_bot.modules.ledger._internal import operator_resolution as resolution_module
from bfx_funding_bot.modules.ledger.tables import ExecutionResolutionJournalRow
from bfx_funding_bot.modules.ledger.wiring import (
    build_operator_evidence,
    build_operator_reads,
    build_operator_resolution,
)

from .test_ledger_capital_reader import (
    book,  # noqa: F401 - fixture re-export
)
from .test_ledger_schema_roles import (
    ledger_db,  # noqa: F401 - fixture re-export
)
from .test_ledger_unknown_resolver_pg import (
    JOURNAL,
    SCOPE,
    STARTED,
    UNKNOWN_AT,
    Clock,
    FakeVenue,
    resolutions,
    run_cycle,
    seed_unknown,
    start,
    venue_offer,
)

pytestmark = pytest.mark.integration
OPERATOR = "operator-1"
RESOLUTION = build_operator_resolution()
READS = build_operator_reads()
RSCOPE = ResolutionScope(SCOPE.exchange_account_id, SCOPE.deployment_environment)
# After the UNKNOWN (1_050_000) and inside the settle window (STARTED + 120_000): the
# automatic resolver leaves the attempt open, so only an operator can resolve it.
OPEN_AT = 1_100_000
LATER = OPEN_AT + 1_000  # still inside the settle window
WORKER_NOW = 2_000_000


class Allow:
    async def __call__(self, session, *, account_id, user) -> bool:
        return True


def make_worker(factory: Any, resolution: Any = RESOLUTION) -> UncertaintyResolutionWorker:
    return UncertaintyResolutionWorker(
        session_factory=factory, scope=RSCOPE, authority=Allow(), clock=lambda: WORKER_NOW,
        resolution=resolution,
    )


async def open_unknown(
    book_: Any, *, offers: tuple[Any, ...] = (), past: tuple[OfferHistory, ...] = (),
    symbols: frozenset[str] | None = None,
) -> tuple[UUID, str]:
    """An UNKNOWN attempt and the ref of an observation that began after it (not settled)."""
    await start(book_)
    attempt = await seed_unknown(book_)
    clock = Clock()
    venue = FakeVenue(clock, offers=offers, past=past, symbols=symbols)
    result = await run_cycle(book_, venue, clock, OPEN_AT)
    assert result.decision == "accepted" and result.resolutions == ()
    assert result.observation_id is not None
    return attempt, observation_evidence_ref(result.observation_id)


def intent(
    uncertainty: UUID, ref: str, action: str = "mark_not_accepted", **changes: Any
) -> ResolutionIntent:
    return replace(
        ResolutionIntent(uncertainty, action, ref, OPERATOR),  # type: ignore[arg-type]
        **changes,
    )


async def queue(factory: Any, item: ResolutionIntent, resolution: Any = RESOLUTION) -> Any:
    async with factory.begin() as session:
        row = await UncertaintyResolutionRequests(RSCOPE, resolution).request(
            session, item, now_ms=1_500_000
        )
        return row.request_id


async def request_row(factory: Any, request_id: UUID) -> UncertaintyResolutionRequestRow:
    async with factory() as session:
        return await UncertaintyResolutionRequests(RSCOPE).get(session, request_id)


async def settle(factory: Any, request_id: UUID, resolution: Any = RESOLUTION) -> str:
    return await make_worker(factory, resolution).process(request_id)


async def state_of(factory: Any, uncertainty: UUID) -> Any:
    async with factory() as session:
        return await READS.get_uncertainty(session, SCOPE, uncertainty)


# --- the happy paths -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_not_accepted_resolves_through_the_worker_and_writes_only_the_journal(book) -> None:  # noqa: F811
    """Mutations 2 and 3 (journal row, its request link), 6."""
    attempt, ref = await open_unknown(book)
    request_id = await queue(book.factory, intent(attempt, ref, reason=" confirmed absent "))
    waiting = await request_row(book.factory, request_id)
    # Mutation 6: the ledger ref lands in observation_id.
    assert waiting.observation_id == UUID(ref.rsplit(":", 1)[1])
    assert waiting.state == "requested"

    assert await settle(book.factory, request_id) == "applied"

    done = await request_row(book.factory, request_id)
    assert (done.state, done.outcome_reason) == ("applied", None)
    (stored,) = await resolutions(book)
    assert (stored.attempt_id, stored.quarantine_id) == (attempt, None)
    assert (stored.action, stored.venue_offer_id, stored.candidate_count) == (
        "not_accepted", None, 0)
    assert (stored.actor_kind, stored.actor_id) == ("operator", OPERATOR)
    assert stored.operator_request_id == request_id
    assert stored.observation_id == waiting.observation_id
    assert stored.reason == "confirmed absent" and stored.resolved_at_ms == WORKER_NOW
    assert stored.evidence == {
        "evidence_ref": ref, "query_started_at_ms": OPEN_AT,
        "query_finished_at_ms": OPEN_AT + 100, "match_kind": "zero_match", "candidate_count": 0,
    }
    view = await state_of(book.factory, attempt)
    assert (view.state, view.resolved_by_operator_id, view.resolution_reason) == (
        "resolved", OPERATOR, "confirmed absent")


@pytest.mark.asyncio
async def test_bind_to_venue_resolves_with_the_matched_offer(book) -> None:  # noqa: F811
    attempt, ref = await open_unknown(book, offers=(venue_offer("V1"),))
    request_id = await queue(
        book.factory, intent(attempt, ref, "bind_to_venue", venue_offer_id="V1"))
    assert await settle(book.factory, request_id) == "applied"
    (stored,) = await resolutions(book)
    assert (stored.action, stored.venue_offer_id, stored.candidate_count) == (
        "bound_to_venue", "V1", 1)
    assert stored.reason == "bind_to_venue_offer"
    assert stored.evidence["venue_status"] == "active" and stored.evidence["match_kind"] == "exact_match"
    assert stored.operator_request_id == request_id


@pytest.mark.asyncio
async def test_manual_resolution_of_a_quarantine(book) -> None:  # noqa: F811
    await start(book, at=1)
    quarantine = uuid4()
    async with book.factory.begin() as session:
        await JOURNAL.open_quarantine(
            session, SCOPE, Quarantine(quarantine, "fUST", Decimal("10"), 0, {}))
    ref = observation_evidence_ref(book.observation_id)  # type: ignore[arg-type]
    request_id = await queue(book.factory, intent(
        quarantine, ref, "manual_resolution", decision="closed_at_venue", reason="checked"))
    assert await settle(book.factory, request_id) == "applied"
    (stored,) = await resolutions(book)
    assert (stored.quarantine_id, stored.attempt_id, stored.action) == (quarantine, None, "manual")
    assert (stored.candidate_count, stored.reason) == (None, "checked")
    assert stored.evidence["decision"] == "closed_at_venue"
    assert (await state_of(book.factory, quarantine)).state == "resolved"


# --- the venue match: refused at queue time like legacy, re-judged by the worker -----------------------


async def apply_directly(book_: Any, item: ResolutionIntent, ref: str) -> None:
    """The worker's ``apply`` on a request that got past (or around) the queue-time check."""
    async with book_.factory.begin() as session:
        await RESOLUTION.apply(
            session, SCOPE, QueuedResolution(uuid4(), item, RESOLUTION.columns(ref)),
            now_ms=WORKER_NOW,
        )


@pytest.mark.asyncio
async def test_a_bind_naming_another_offer_is_refused_when_queued_and_by_the_worker(book) -> None:  # noqa: F811
    attempt, ref = await open_unknown(book, offers=(venue_offer("V1"),))
    wrong = intent(attempt, ref, "bind_to_venue", venue_offer_id="V9")
    with pytest.raises(RequestRefused, match="venue_offer_match_not_exact"):
        await queue(book.factory, wrong)
    # The worker does not trust the queue: the same request applied directly is refused too.
    with pytest.raises(ResolutionRejected, match="venue_offer_match_not_exact"):
        await apply_directly(book, wrong, ref)
    assert await resolutions(book) == []


@pytest.mark.asyncio
async def test_bind_refuses_multiple_matches(book) -> None:  # noqa: F811
    """Mutation 7 (original) / 5 (3e4b): an operator bind accepting ``multiple_match``."""
    attempt, ref = await open_unknown(book, offers=(venue_offer("V1"), venue_offer("V2")))
    bind = intent(attempt, ref, "bind_to_venue", venue_offer_id="V1")
    with pytest.raises(RequestRefused, match="venue_offer_match_not_exact"):
        await queue(book.factory, bind)
    with pytest.raises(ResolutionRejected, match="venue_offer_match_not_exact"):
        await apply_directly(book, bind, ref)
    assert await resolutions(book) == []


@pytest.mark.asyncio
async def test_mark_not_accepted_refuses_when_an_offer_matches(book) -> None:  # noqa: F811
    """not_accepted needs a ``zero_match``: an exact candidate is not absence."""
    attempt, ref = await open_unknown(book, offers=(venue_offer("V1"),))
    with pytest.raises(RequestRefused, match="venue_offer_match_not_zero"):
        await queue(book.factory, intent(attempt, ref))
    with pytest.raises(ResolutionRejected, match="venue_offer_match_not_zero"):
        await apply_directly(book, intent(attempt, ref), ref)


@pytest.mark.asyncio
async def test_mark_not_accepted_refuses_incomplete_evidence(book) -> None:  # noqa: F811
    """Mutation 8: the same verdict when the history was never fetched for the symbol."""
    attempt, ref = await open_unknown(book, symbols=frozenset({"fUSD"}))
    with pytest.raises(RequestRefused, match="venue_offer_match_not_zero"):
        await queue(book.factory, intent(attempt, ref))
    with pytest.raises(ResolutionRejected, match="venue_offer_match_not_zero"):
        await apply_directly(book, intent(attempt, ref), ref)
    assert await resolutions(book) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["mark_not_accepted", "bind_to_venue"])
async def test_the_worker_checks_the_payload_digest_the_preview_cannot(book, action) -> None:  # noqa: F811
    """Mutation 7 (3e4b): ``apply`` skipping the digest recheck.

    A payload whose digest no longer matches passes the web API's preview (generated
    columns, no digest) and is queued; the worker, which reads the payload, refuses it.
    """
    attempt, ref = await open_unknown(book, offers=(venue_offer("V1"),))
    item = intent(attempt, ref, action, venue_offer_id="V1" if action == "bind_to_venue" else None)
    if action == "mark_not_accepted":
        # No offer is wanted for the zero verdict: forge it so the preview alone says zero.
        async with book.factory.begin() as session:
            await session.execute(text("ALTER TABLE submission_attempt_journal DISABLE TRIGGER USER"))
            await session.execute(text(
                "UPDATE submission_attempt_journal SET normalized_payload = jsonb_set("
                "normalized_payload, '{rate}', '\"0.9\"')"))
            await session.execute(text("ALTER TABLE submission_attempt_journal ENABLE TRIGGER USER"))
        request_id = await queue(book.factory, item)  # preview: the (forged) rate matches no offer
        code = "venue_offer_match_not_zero"
    else:
        async with book.factory.begin() as session:
            await session.execute(text("ALTER TABLE submission_attempt_journal DISABLE TRIGGER USER"))
            await session.execute(text(
                "UPDATE submission_attempt_journal SET payload_sha256 = repeat('0', 64)"))
            await session.execute(text("ALTER TABLE submission_attempt_journal ENABLE TRIGGER USER"))
        request_id = await queue(book.factory, item)  # preview: terms unchanged, so V1 is exact
        code = "venue_offer_match_not_exact"
    assert await settle(book.factory, request_id) == "rejected"
    assert (await request_row(book.factory, request_id)).outcome_reason == code
    assert await resolutions(book) == []


# --- request-time checks and idempotency -------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_resolution_of_an_attempt_is_refused(book) -> None:  # noqa: F811
    """Mutation 11: manual_resolution allowed on an attempt (the journal's rule is the backstop)."""
    attempt, ref = await open_unknown(book)
    manual = intent(attempt, ref, "manual_resolution", decision="closed_at_venue", reason="x")
    with pytest.raises(RequestRefused, match="resolution_action_not_supported"):
        await queue(book.factory, manual)
    # The worker repeats the check on a row that got past the web API.
    async with book.factory.begin() as session:
        with pytest.raises(ResolutionRejected, match="resolution_action_not_supported"):
            await RESOLUTION.apply(
                session, SCOPE,
                QueuedResolution(uuid4(), manual, RESOLUTION.columns(ref)), now_ms=WORKER_NOW)
    assert await resolutions(book) == []


@pytest.mark.asyncio
async def test_bind_and_not_accepted_are_refused_for_a_quarantine(book) -> None:  # noqa: F811
    await start(book, at=1)
    quarantine = uuid4()
    async with book.factory.begin() as session:
        await JOURNAL.open_quarantine(
            session, SCOPE, Quarantine(quarantine, "fUST", Decimal("10"), 0, {}))
    ref = observation_evidence_ref(book.observation_id)  # type: ignore[arg-type]
    for action in ("mark_not_accepted", "bind_to_venue"):
        with pytest.raises(RequestRefused, match="resolution_action_not_supported"):
            await queue(book.factory, intent(quarantine, ref, action, venue_offer_id="V1"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"decision": "nonsense", "reason": "r"}, "invalid_manual_resolution_decision"),
        ({"decision": "closed_at_venue", "reason": "  "}, "operator_reason_required"),
    ],
)
async def test_manual_resolution_needs_a_known_decision_and_a_reason(book, changes, code) -> None:  # noqa: F811
    await start(book, at=1)
    quarantine = uuid4()
    async with book.factory.begin() as session:
        await JOURNAL.open_quarantine(
            session, SCOPE, Quarantine(quarantine, "fUST", Decimal("10"), 0, {}))
    ref = observation_evidence_ref(book.observation_id)  # type: ignore[arg-type]
    with pytest.raises(RequestRefused, match=code) as raised:
        await queue(book.factory, intent(quarantine, ref, "manual_resolution", **changes))
    assert raised.value.kind == "invalid"


@pytest.mark.asyncio
async def test_unknown_or_foreign_uncertainty_and_malformed_ref(book) -> None:  # noqa: F811
    attempt, ref = await open_unknown(book)
    with pytest.raises(RequestRefused, match="not_found") as missing:
        await queue(book.factory, intent(uuid4(), ref))
    assert missing.value.kind == "not_found"
    for bad in ("42", "ledger:v1:obs:nope", ref.upper()):
        with pytest.raises(RequestRefused, match="stale_reconcile_fence"):
            await queue(book.factory, intent(attempt, bad))
    assert await resolutions(book) == []


@pytest.mark.asyncio
async def test_pending_conflict_and_same_intent(book) -> None:  # noqa: F811
    attempt, ref = await open_unknown(book, offers=(venue_offer("V1"),))
    first = await queue(book.factory, intent(attempt, ref, "bind_to_venue", venue_offer_id="V1"))
    again = await queue(book.factory, intent(attempt, ref, "bind_to_venue", venue_offer_id="V1"))
    assert again == first  # a double click is not a second adjudication
    with pytest.raises(ResolutionRequestPending):
        await queue(book.factory, intent(attempt, ref))
    # The same action citing another observation is a different intent.
    other = observation_evidence_ref(uuid4())
    with pytest.raises(ResolutionRequestPending):
        await queue(book.factory, intent(attempt, other, "bind_to_venue", venue_offer_id="V1"))
    assert await settle(book.factory, first) == "applied"
    with pytest.raises(RequestRefused, match="uncertainty_already_resolved"):
        await queue(book.factory, intent(attempt, ref))


# --- the fence and a concurrent resolution ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_newer_accepted_observation_makes_the_request_stale(book) -> None:  # noqa: F811
    """Mutation 5: a stale fence is a rejected request, not a failed one."""
    attempt, ref = await open_unknown(book)
    request_id = await queue(book.factory, intent(attempt, ref))
    clock = Clock()
    newer = await run_cycle(book, FakeVenue(clock), clock, LATER)
    assert newer.decision == "accepted"
    assert await resolutions(book) == []
    assert await settle(book.factory, request_id) == "rejected"
    row = await request_row(book.factory, request_id)
    assert (row.state, row.outcome_reason) == ("rejected", "stale_reconcile_fence")
    assert (await state_of(book.factory, attempt)).state == "open"


@pytest.mark.asyncio
async def test_a_subject_resolved_meanwhile_is_a_rejected_request(book) -> None:  # noqa: F811
    attempt, ref = await open_unknown(book)
    request_id = await queue(book.factory, intent(attempt, ref))
    async with book.factory.begin() as session:
        await JOURNAL.record_resolution(session, SCOPE, Resolution(
            uuid4(), "fUST", "not_accepted", None, UUID(ref.rsplit(":", 1)[1]), "system",
            "system:reconcile", WORKER_NOW - 1, "r", {}, attempt_id=attempt))
    assert await settle(book.factory, request_id) == "rejected"
    row = await request_row(book.factory, request_id)
    assert row.outcome_reason == "uncertainty_already_resolved"
    assert len(await resolutions(book)) == 1


@pytest.mark.asyncio
async def test_the_journal_refusing_a_duplicate_is_a_rejected_request(book, monkeypatch) -> None:  # noqa: F811
    """Mutation 4: ``ResolutionAlreadyRecorded`` becoming a failed request.

    The reads see the subject open (as they would before a concurrent commit); the
    journal's own unique rule is the last word.
    """
    attempt, ref = await open_unknown(book)
    request_id = await queue(book.factory, intent(attempt, ref))
    stale_view = await state_of(book.factory, attempt)
    async with book.factory.begin() as session:
        await JOURNAL.record_resolution(session, SCOPE, Resolution(
            uuid4(), "fUST", "not_accepted", None, UUID(ref.rsplit(":", 1)[1]), "system",
            "system:reconcile", WORKER_NOW - 1, "r", {}, attempt_id=attempt))

    async def open_view(self, session, scope, item):
        return stale_view

    monkeypatch.setattr(resolution_module.LedgerOperatorResolution, "_subject", open_view)
    assert await settle(book.factory, request_id) == "rejected"
    row = await request_row(book.factory, request_id)
    assert (row.state, row.outcome_reason) == (REJECTED, "uncertainty_already_resolved")


# --- verify: the uncertainty opens when the UNKNOWN was recorded --------------------------------------


@pytest.mark.asyncio
async def test_verify_opens_an_attempt_at_its_unknown_not_at_its_submit(book) -> None:  # noqa: F811
    """Mutation 10 (G2): ``started_at_ms`` instead of the UNKNOWN outcome's ``completed_at_ms``."""
    await start(book)
    attempt = await seed_unknown(book)  # submit at 1_000_000, UNKNOWN at 1_050_000
    evidence = build_operator_evidence()
    subject = ResolutionSubject(attempt, "fUST", attempt)

    async def judge(query_started: int) -> None:
        assert await book.accept(
            started=query_started, finished=query_started + 1, confirmed=query_started + 3
        ) == "accepted"
        async with book.factory() as session:
            await evidence.verify(
                session, SCOPE, subject, observation_evidence_ref(book.observation_id),  # type: ignore[arg-type]
                require_history=True)

    for before_or_at_unknown in (STARTED + 10, UNKNOWN_AT):
        with pytest.raises(ResolutionRejected, match="stale_reconcile_fence"):
            await judge(before_or_at_unknown)
    await judge(UNKNOWN_AT + 1)


# --- O3: the context an operator cites ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolution_context_cites_the_latest_accepted_observation(book) -> None:  # noqa: F811
    """Mutation 9: a non-latest observation; plus the legacy-shaped match fields."""
    attempt, first = await open_unknown(book, offers=(venue_offer("V1"),))
    evidence = build_operator_evidence()
    subject = ResolutionSubject(attempt, "fUST", attempt)
    async with book.factory() as session:
        context = await evidence.resolution_context(session, SCOPE, subject)
    assert (context.evidence_ref, context.query_started_at_ms, context.query_finished_at_ms) == (
        first, OPEN_AT, OPEN_AT + 100)
    assert (context.candidate_count, context.candidate_venue_offer_ids, context.unavailable_reason) == (
        1, ("V1",), None)

    # A newer observation (two candidates now); the context follows it, not the first.
    clock = Clock()
    newer = await run_cycle(
        book, FakeVenue(clock, offers=(venue_offer("V1"), venue_offer("V2"))), clock, LATER)
    assert newer.observation_id is not None and newer.resolutions == ()
    async with book.factory() as session:
        context = await evidence.resolution_context(session, SCOPE, subject)
    assert context.evidence_ref == observation_evidence_ref(newer.observation_id) != first
    assert (context.candidate_count, context.candidate_venue_offer_ids) == (2, ("V1", "V2"))
    assert context.unavailable_reason == "multiple_exact_candidates"


@pytest.mark.asyncio
async def test_resolution_context_reasons(book) -> None:  # noqa: F811
    attempt, _ref = await open_unknown(book, symbols=frozenset({"fUSD"}))
    subject = ResolutionSubject(attempt, "fUST", attempt)
    async with book.factory() as session:
        context = await build_operator_evidence().resolution_context(session, SCOPE, subject)
    # The symbol's history was never fetched: incomplete, as under legacy.
    assert context.unavailable_reason == "incomplete_match_evidence" and context.candidate_count is None
    assert context.evidence_ref is not None

    with pytest.raises(RequestRefused, match="venue_offer_match_not_zero"):
        await queue(book.factory, intent(attempt, context.evidence_ref))  # no zero match to cite
    async with book.factory.begin() as session:
        await JOURNAL.record_resolution(session, SCOPE, Resolution(
            uuid4(), "fUST", "not_accepted", None, UUID(context.evidence_ref.rsplit(":", 1)[1]),
            "system", "system:reconcile", WORKER_NOW, "r", {}, attempt_id=attempt))
    async with book.factory() as session:
        resolved = await build_operator_evidence().resolution_context(session, SCOPE, subject)
    assert resolved.unavailable_reason == "uncertainty_not_open"


@pytest.mark.asyncio
async def test_resolution_context_without_an_accepted_observation_or_with_a_quarantine(book) -> None:  # noqa: F811
    quarantine = uuid4()
    async with book.factory.begin() as session:
        await JOURNAL.open_quarantine(
            session, SCOPE, Quarantine(quarantine, "fUST", Decimal("10"), 0, {}))
    evidence = build_operator_evidence()
    subject = ResolutionSubject(quarantine, "fUST")
    async with book.factory() as session:
        none_yet = await evidence.resolution_context(session, SCOPE, subject)
    assert none_yet.unavailable_reason == "fresh_reconcile_required" and none_yet.evidence_ref is None
    await start(book, at=1)
    async with book.factory() as session:
        cited = await evidence.resolution_context(session, SCOPE, subject)
        with pytest.raises(ResolutionRejected, match="not_found"):
            await evidence.resolution_context(session, SCOPE, ResolutionSubject(uuid4(), "fUST"))
    assert cited.evidence_ref == observation_evidence_ref(book.observation_id)  # type: ignore[arg-type]
    assert cited.unavailable_reason is None and cited.candidate_count is None


def test_the_manual_decisions_are_the_two_operator_actions() -> None:
    assert {"accepted_external_exposure", "closed_at_venue"} == MANUAL_RESOLUTION_DECISIONS


# --- atomicity ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_crash_after_the_journal_row_leaves_neither_row(book, monkeypatch) -> None:  # noqa: F811
    """Mutation 2/3 (half state): journal + request outcome commit together or not at all."""
    attempt, ref = await open_unknown(book)
    request_id = await queue(book.factory, intent(attempt, ref))
    real = resolution_module.LedgerOperatorResolution.apply

    async def crash(self, session, scope, request, *, now_ms):
        await real(self, session, scope, request, now_ms=now_ms)
        raise KeyboardInterrupt  # the process dies after the journal INSERT, before commit

    monkeypatch.setattr(resolution_module.LedgerOperatorResolution, "apply", crash)
    with pytest.raises(KeyboardInterrupt):
        await settle(book.factory, request_id)
    assert await resolutions(book) == []
    assert (await request_row(book.factory, request_id)).state == "requested"
    monkeypatch.undo()
    assert await settle(book.factory, request_id) == "applied"  # a retry runs from `requested`
    assert len(await resolutions(book)) == 1


@pytest.mark.asyncio
async def test_applied_without_its_journal_row_is_refused_by_the_database(book, monkeypatch) -> None:  # noqa: F811
    """Mutation 2: an applied ledger request needs its journal row (REPLACE guard)."""
    attempt, ref = await open_unknown(book)
    request_id = await queue(book.factory, intent(attempt, ref))

    async def skip(session, scope, resolution):
        return None

    monkeypatch.setattr(resolution_module, "record_resolution", skip)
    assert await settle(book.factory, request_id) == "failed"
    row = await request_row(book.factory, request_id)
    assert row.state == FAILED and "outcome_write_failed" in (row.outcome_reason or "")
    assert await resolutions(book) == []


@pytest.mark.asyncio
async def test_a_second_request_cannot_reuse_a_journal_row(book) -> None:  # noqa: F811
    """The partial UNIQUE on ``operator_request_id`` backs the idempotency."""
    attempt, ref = await open_unknown(book)
    request_id = await queue(book.factory, intent(attempt, ref))
    assert await settle(book.factory, request_id) == "applied"
    async with book.factory() as session:
        stored = (await session.scalars(select(ExecutionResolutionJournalRow))).one()
        assert stored.operator_request_id == request_id
        assert await session.scalar(
            text("SELECT count(*) FROM execution_resolution_journal WHERE operator_request_id=:r"),
            {"r": request_id}) == 1


# --- real roles: the web API validates and queues, the bot applies ----------------------------------------


def _role_factory(ledger_db, role: str):  # noqa: F811
    """Sessions whose every transaction runs as ``role`` (SET LOCAL: a pooled connection's
    session-level SET ROLE is rolled back on check-in)."""
    engine = create_async_engine(
        ledger_db.url.render_as_string(hide_password=False).replace("+psycopg", "+asyncpg"))

    class RoleSession(Session):
        pass

    @event.listens_for(RoleSession, "after_begin")
    def _set_role(_session, _transaction, connection) -> None:
        connection.exec_driver_sql(f"SET LOCAL ROLE {role}")

    return engine, async_sessionmaker(engine, expire_on_commit=False, sync_session_class=RoleSession)


@pytest.mark.asyncio
async def test_the_web_api_role_queues_and_the_bot_role_applies(book, ledger_db) -> None:  # noqa: F811
    """No new grant: prepare reads only allowlisted columns."""
    attempt, ref = await open_unknown(book, offers=(venue_offer("V1"),))
    web, web_factory = _role_factory(ledger_db, "bfx_webapi")
    bot, bot_factory = _role_factory(ledger_db, "bfx_bot")
    try:
        bind = intent(attempt, ref, "bind_to_venue", venue_offer_id="V1")
        request_id = await queue(web_factory, bind)
        assert await settle(bot_factory, request_id) == "applied"
        done = await request_row(book.factory, request_id)
        assert done.state == "applied"
        (stored,) = await resolutions(book)
        assert (stored.action, stored.venue_offer_id, stored.operator_request_id) == (
            "bound_to_venue", "V1", request_id)
        # The cited state is visible to the web API's own reads.
        async with web_factory() as session:
            view = await READS.get_uncertainty(session, SCOPE, attempt)
        assert view is not None and view.state == "resolved"
    finally:
        await web.dispose()
        await bot.dispose()


async def _web_context(factory: Any, attempt: UUID) -> Any:
    async with factory() as session:
        return await build_operator_evidence().resolution_context(
            session, SCOPE, ResolutionSubject(attempt, "fUST", attempt))


@pytest.mark.asyncio
async def test_the_web_api_role_builds_the_context_and_never_reads_the_payloads(book, ledger_db) -> None:  # noqa: F811
    """3e4b: ``bfx_webapi`` derives the candidates from granted columns (matching the owner's
    view of the same scenario) and is denied ``raw``, ``normalized_payload`` and ``evidence``."""
    attempt, ref = await open_unknown(book, offers=(venue_offer("V1"), venue_offer("V2")))
    web, web_factory = _role_factory(ledger_db, "bfx_webapi")
    try:
        as_web = await _web_context(web_factory, attempt)
        as_owner = await _web_context(book.factory, attempt)
        assert as_web == as_owner
        assert (as_web.evidence_ref, as_web.candidate_count, as_web.candidate_venue_offer_ids,
                as_web.unavailable_reason) == (ref, 2, ("V1", "V2"), "multiple_exact_candidates")
        for statement in (
            "SELECT raw FROM ledger_observation_offer",
            "SELECT raw FROM ledger_observation_offer_history",
            "SELECT normalized_payload FROM submission_attempt_journal",
            "SELECT evidence FROM ledger_observation",
            "SELECT payload_sha256 FROM submission_attempt_journal",
        ):
            with pytest.raises(Exception, match="permission denied"):
                async with web_factory.begin() as session:
                    await session.execute(text(statement))
    finally:
        await web.dispose()


@pytest.mark.asyncio
async def test_the_web_api_role_sees_the_history_symbol_check(book, ledger_db) -> None:  # noqa: F811
    """3e4b mutation 4: a symbol the port never fetched stays ``incomplete`` under the web role."""
    attempt, _ref = await open_unknown(book, symbols=frozenset({"fUSD"}))
    web, web_factory = _role_factory(ledger_db, "bfx_webapi")
    try:
        context = await _web_context(web_factory, attempt)
    finally:
        await web.dispose()
    assert context.unavailable_reason == "incomplete_match_evidence"
    assert (context.candidate_count, context.candidate_venue_offer_ids) == (None, ())


@pytest.mark.asyncio
async def test_the_web_api_role_refuses_a_wrong_bind_when_queueing(book, ledger_db) -> None:  # noqa: F811
    """3e4b: the queue-time operator rule runs on granted columns only (and a good bind queues)."""
    attempt, ref = await open_unknown(book, offers=(venue_offer("V1"), venue_offer("V2")))
    web, web_factory = _role_factory(ledger_db, "bfx_webapi")
    try:
        # Two exact candidates: neither bind nor not-accepted may be queued.
        with pytest.raises(RequestRefused, match="venue_offer_match_not_exact"):
            await queue(web_factory, intent(attempt, ref, "bind_to_venue", venue_offer_id="V1"))
        with pytest.raises(RequestRefused, match="venue_offer_match_not_zero"):
            await queue(web_factory, intent(attempt, ref))
    finally:
        await web.dispose()
