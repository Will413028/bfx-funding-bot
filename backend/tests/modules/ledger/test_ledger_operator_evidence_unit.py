"""Guard mutations must fail the correspondingly named test in this module."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from bfx_funding_bot.modules.ledger import ResolutionRejected, ResolutionSubject, Scope
from bfx_funding_bot.modules.ledger._internal.operator_evidence import observation_id
from bfx_funding_bot.modules.ledger.wiring import build_operator_evidence

SCOPE = Scope(uuid4(), "ci")
ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
SUBJECT = ResolutionSubject(uuid4(), "fUST")
TOKEN = f"ledger:v1:obs:{ID}"


def evidence_session():
    observation = SimpleNamespace(
        id=ID,
        query_id=uuid4(),
        exchange_account_id=SCOPE.exchange_account_id,
        deployment_environment="ci",
        accepted=True,
        first_digest="digest",
        confirmation_digest="digest",
        query_finished_at_ms=30,
        wallets_complete=True,
        offers_complete=True,
        credits_complete=True,
        loans_complete=True,
        offer_history_complete=True,
        credit_history_complete=True,
        trades_complete=True,
    )
    opening = SimpleNamespace(
        exchange_account_id=SCOPE.exchange_account_id,
        deployment_environment="ci",
        symbol="fUST",
        opened_at_ms=10,
    )
    query = SimpleNamespace(
        exchange_account_id=SCOPE.exchange_account_id, deployment_environment="ci", started_at_ms=20
    )
    session = AsyncMock()
    # ``verify`` selects explicit columns (one row each): observation, opening, query.
    session.execute.side_effect = [
        MagicMock(one_or_none=MagicMock(return_value=row)) for row in (observation, opening, query)
    ]
    session.scalar.return_value = observation
    return session, observation, opening, query


@pytest.mark.asyncio
async def test_ledger_verifies_complete_latest_accepted_evidence():
    session, *_ = evidence_session()
    result = await build_operator_evidence().verify(
        session, SCOPE, SUBJECT, TOKEN, require_history=True
    )
    assert (result.evidence_ref, result.query_started_at_ms, result.query_finished_at_ms) == (
        TOKEN,
        20,
        30,
    )
    # Column-grant safe: no whole-row ``session.get`` (it names every column).
    session.get.assert_not_called()


@pytest.mark.asyncio
async def test_ledger_rejects_non_latest_observation():
    session, *_ = evidence_session()
    session.scalar.return_value = SimpleNamespace(id=uuid4())
    with pytest.raises(ResolutionRejected, match="stale_reconcile_fence"):
        await build_operator_evidence().verify(session, SCOPE, SUBJECT, TOKEN, require_history=True)


@pytest.mark.asyncio
async def test_ledger_rejects_unaccepted_observation_even_if_latest_result_is_it():
    session, observation, *_ = evidence_session()
    observation.accepted = False
    with pytest.raises(ResolutionRejected, match="stale_reconcile_fence"):
        await build_operator_evidence().verify(session, SCOPE, SUBJECT, TOKEN, require_history=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("require_history", [True, False])
async def test_ledger_requires_offer_history_complete(require_history):
    session, observation, *_ = evidence_session()
    observation.offer_history_complete = False
    with pytest.raises(ResolutionRejected, match="incomplete_offer_history_coverage"):
        await build_operator_evidence().verify(
            session, SCOPE, SUBJECT, TOKEN, require_history=require_history
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "wallets_complete",
        "offers_complete",
        "credits_complete",
        "loans_complete",
        "credit_history_complete",
        "trades_complete",
    ],
)
async def test_ledger_requires_every_coverage_flag(field):
    session, observation, *_ = evidence_session()
    setattr(observation, field, False)
    with pytest.raises(ResolutionRejected, match="incomplete_reconcile_coverage"):
        await build_operator_evidence().verify(
            session, SCOPE, SUBJECT, TOKEN, require_history=False
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["digest", "opening", "observation_scope", "query_scope", "subject_scope"]
)
async def test_ledger_rejects_invalid_evidence(fault):
    session, observation, opening, query = evidence_session()
    if fault == "digest":
        observation.confirmation_digest = "different"
    elif fault == "opening":
        query.started_at_ms = opening.opened_at_ms
    elif fault == "observation_scope":
        observation.exchange_account_id = uuid4()
    elif fault == "query_scope":
        query.deployment_environment = "other"
    else:
        opening.symbol = "fUSD"
    with pytest.raises(ResolutionRejected):
        await build_operator_evidence().verify(session, SCOPE, SUBJECT, TOKEN, require_history=True)


@pytest.mark.parametrize(
    "token",
    [
        "42",
        "042",
        TOKEN.upper(),
        f"ledger:v1:obs:{str(ID).upper()}",
        f"ledger:v1:obs:{ID.hex}",
        f"ledger:v1:obs:{{{ID}}}",
        f" {TOKEN}",
    ],
)
def test_ledger_token_parser_rejects_noncanonical_uuid_and_legacy_tokens(token):
    with pytest.raises(ResolutionRejected, match="stale_reconcile_fence"):
        observation_id(token)


@pytest.mark.asyncio
async def test_dormant_context_is_explicitly_matcher_pending_without_database_reads():
    session = AsyncMock()
    result = await build_operator_evidence().resolution_context(session, SCOPE, SUBJECT)
    assert result.evidence_ref is None
    assert result.candidate_count is None
    assert result.candidate_venue_offer_ids == ()
    assert result.unavailable_reason == "matcher_pending"
    session.scalar.assert_not_called()
    session.get.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["not_latest", "not_accepted"])
async def test_p3_journal_authoritatively_rejects_invalid_observation(monkeypatch, fault):
    from bfx_funding_bot.modules.ledger import Resolution
    from bfx_funding_bot.modules.ledger._internal import journal

    session, observation, opening, query = evidence_session()
    session.get.side_effect = [opening, observation, query]
    latest = observation
    if fault == "not_latest":
        latest = SimpleNamespace(id=uuid4())
    else:
        observation.accepted = False
    session.scalar.side_effect = [None, latest]
    monkeypatch.setattr(journal, "lock_scope", AsyncMock())
    monkeypatch.setattr(journal, "bump_locked", AsyncMock())
    resolution = Resolution(
        uuid4(),
        "fUST",
        "manual",
        None,
        ID,
        "operator",
        "test",
        40,
        "verified",
        {},
        quarantine_id=SUBJECT.uncertainty_id,
    )
    with pytest.raises(ResolutionRejected, match="latest accepted"):
        await journal.record_resolution(session, SCOPE, resolution)
    session.add.assert_not_called()
    journal.bump_locked.assert_not_called()
