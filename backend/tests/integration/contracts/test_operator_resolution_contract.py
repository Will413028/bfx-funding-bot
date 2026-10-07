"""OperatorResolution: the request-path suite on the ledger authority.

A request carries the ``observation_id`` it cites; the applied record is one journal row
(citing the request) and never the event log.

A bind naming the wrong offer and a not-accepted over a non-zero match are refused when
the request is queued (S1-3e4b: the web API previews the match from granted columns); the
worker re-judges every request regardless.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from bfx_funding_bot.modules.execution.uncertainty_requests import (
    ResolutionRejected,
    ResolutionRequestPending,
)
from bfx_funding_bot.modules.ledger.tables import ExecutionResolutionJournalRow

from .conftest import port_stack  # noqa: F401 - fixture re-export
from .operator import OPERATOR, Driver, build_driver

pytestmark = pytest.mark.integration


@pytest.fixture
def driver(port_stack) -> Driver:  # noqa: F811
    return build_driver(port_stack)


async def journal(driver: Driver) -> list[ExecutionResolutionJournalRow]:
    async with driver.factory() as session:
        return list(await session.scalars(select(ExecutionResolutionJournalRow)))


async def refusal(driver: Driver, item) -> str:
    """The code that refuses this request at queue time."""
    with pytest.raises(ResolutionRejected) as refused:
        await driver.request(item)
    return refused.value.code


@pytest.mark.asyncio
async def test_mark_not_accepted_resolves_the_subject(driver) -> None:
    uncertainty = await driver.open_unknown()
    ref = await driver.observe()
    assert (await driver.view(uncertainty)).state == "open"

    waiting = await driver.request(driver.intent(uncertainty, ref, reason="absent"))
    assert waiting.state == "requested" and waiting.requested_by == OPERATOR
    done = await driver.settle(waiting.request_id)

    assert (done.state, done.outcome_reason) == ("applied", None)
    view = await driver.view(uncertainty)
    assert (view.state, view.resolved_by_operator_id, view.resolution_reason) == (
        "resolved", OPERATOR, "absent")
    # Mutations 2, 3, 6: the observation, a journal row of this request.
    assert waiting.observation_id == UUID(ref.rsplit(":", 1)[1])
    (stored,) = await journal(driver)
    assert (stored.attempt_id, stored.action, stored.operator_request_id) == (
        uncertainty, "not_accepted", waiting.request_id)


@pytest.mark.asyncio
async def test_bind_to_venue_resolves_the_subject(driver) -> None:
    uncertainty = await driver.open_unknown()
    ref = await driver.observe(("V1",))
    waiting = await driver.request(
        driver.intent(uncertainty, ref, "bind_to_venue", venue_offer_id="V1"))
    done = await driver.settle(waiting.request_id)
    assert (done.state, done.outcome_reason) == ("applied", None)
    assert (await driver.view(uncertainty)).state == "resolved"
    (stored,) = await journal(driver)
    assert (stored.action, stored.venue_offer_id) == ("bound_to_venue", "V1")


@pytest.mark.asyncio
async def test_a_bind_naming_another_offer_never_resolves(driver) -> None:
    uncertainty = await driver.open_unknown()
    ref = await driver.observe(("V1",))
    code = await refusal(
        driver, driver.intent(uncertainty, ref, "bind_to_venue", venue_offer_id="V9"))
    assert code == "venue_offer_match_not_exact"
    assert (await driver.view(uncertainty)).state == "open"
    assert await journal(driver) == []


@pytest.mark.asyncio
async def test_not_accepted_is_refused_while_an_offer_matches(driver) -> None:
    uncertainty = await driver.open_unknown()
    ref = await driver.observe(("V1",))
    assert await refusal(driver, driver.intent(uncertainty, ref)) == "venue_offer_match_not_zero"
    assert (await driver.view(uncertainty)).state == "open"


@pytest.mark.asyncio
async def test_a_manual_resolution_of_an_attempt_is_refused(driver) -> None:
    uncertainty = await driver.open_unknown()
    ref = await driver.observe()
    manual = driver.intent(
        uncertainty, ref, "manual_resolution", decision="closed_at_venue", reason="by hand")
    with pytest.raises(ResolutionRejected, match="resolution_action_not_supported"):
        await driver.request(manual)
    assert (await driver.view(uncertainty)).state == "open"


@pytest.mark.asyncio
async def test_pending_conflict_and_same_intent(driver) -> None:
    uncertainty = await driver.open_unknown()
    ref = await driver.observe(("V1",))
    bind = driver.intent(uncertainty, ref, "bind_to_venue", venue_offer_id="V1")
    first = await driver.request(bind)
    assert (await driver.request(bind)).request_id == first.request_id
    with pytest.raises(ResolutionRequestPending):
        await driver.request(driver.intent(uncertainty, ref))
    # Another reference is another intent, not the waiting one.
    with pytest.raises(ResolutionRequestPending):
        await driver.request(driver.intent(
            uncertainty, driver.another_ref(), "bind_to_venue", venue_offer_id="V1"))
    assert (await driver.settle(first.request_id)).state == "applied"


@pytest.mark.asyncio
async def test_a_newer_observation_makes_a_queued_request_stale(driver) -> None:
    """Mutation 5: the stale fence is a rejection with its bounded code, not a failure."""
    uncertainty = await driver.open_unknown()
    ref = await driver.observe()
    waiting = await driver.request(driver.intent(uncertainty, ref))
    await driver.observe()
    done = await driver.settle(waiting.request_id)
    assert (done.state, done.outcome_reason) == ("rejected", "stale_reconcile_fence")
    assert (await driver.view(uncertainty)).state == "open"


@pytest.mark.asyncio
async def test_a_resolved_subject_refuses_the_next_request(driver) -> None:
    uncertainty = await driver.open_unknown()
    ref = await driver.observe()
    first = await driver.request(driver.intent(uncertainty, ref))
    assert (await driver.settle(first.request_id)).state == "applied"
    with pytest.raises(ResolutionRejected, match="uncertainty_already_resolved"):
        await driver.request(driver.intent(uncertainty, ref))


@pytest.mark.asyncio
async def test_malformed_foreign_and_unknown_requests(driver) -> None:
    uncertainty = await driver.open_unknown()
    ref = await driver.observe()
    for bad in ("nope", "", driver.other_authority_ref()):
        with pytest.raises(ResolutionRejected, match="stale_reconcile_fence"):
            await driver.request(driver.intent(uncertainty, bad))
    with pytest.raises(ResolutionRejected, match="not_found") as missing:
        await driver.request(driver.intent(uuid4(), ref))
    assert missing.value.kind == "not_found"
    assert (await driver.view(uncertainty)).state == "open"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("offers", "count", "ids", "reason"),
    [
        ((), 0, (), None),
        (("V1",), 1, ("V1",), None),
        (("V1", "V2"), 2, ("V1", "V2"), "multiple_exact_candidates"),
    ],
)
async def test_the_resolution_context_carries_the_same_candidates(
    driver, offers, count, ids, reason
) -> None:
    """The preview's candidate fields (count, offer ids, reason) for one scenario."""
    uncertainty = await driver.open_unknown()
    ref = await driver.observe(offers)
    context = await driver.context(uncertainty)
    assert context.evidence_ref == ref
    assert (context.candidate_count, context.candidate_venue_offer_ids, context.unavailable_reason) == (
        count, ids, reason)
