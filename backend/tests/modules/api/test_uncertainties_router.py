"""Account-scoped uncertainty API contract tests.

The public router through FastAPI, on SQLite for the request outbox it owns. What an
uncertainty is, which evidence is fresh and what a resolution writes belong to the ledger's
ports (``OperatorReads`` / ``OperatorEvidence`` / ``OperatorResolution``), covered on
PostgreSQL by ``tests/integration/test_ledger_operator_resolution_pg.py`` and
``tests/integration/test_ledger_operator_reads.py``; here a scripted stand-in plays them, so
these tests pin the router and the outbox: scoping, the wire shape, the HTTP codes of a
refusal, the queue's same-request / pending semantics and the worker's outcome recording.

Mutation checks (one at a time; revert after each):

* ``_resolution_context`` passes the first candidate only, or the port's count no longer:
  ``test_uncertainty_read_renders_*``.
* ``_queue`` skips ``resolution.columns``: ``test_a_malformed_evidence_ref_is_*``.
* ``_same_intent`` ignores the reason: ``test_identical_repeat_*``.
* the worker trusts acceptance instead of re-reading the operator: ``test_revoked_operator_*``.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.accounts.tables
import bfx_funding_bot.modules.execution.audit.tables
import bfx_funding_bot.modules.execution.uncertainty_tables
import bfx_funding_bot.modules.ledger.tables  # noqa: F401
from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.exchange_accounts import grant_membership
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.api.deps import ReadModels, get_session
from bfx_funding_bot.modules.ledger import (
    AppliedResolution,
    RequestColumns,
    ResolutionEvidence,
    ResolutionRejected,
    UncertaintyView,
    observation_evidence_ref,
)

ACCOUNT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
OTHER_ACCOUNT_ID = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
SCID = UUID("11111111-1111-1111-1111-111111111111")
UNCERTAINTY_ID = UUID("22222222-2222-2222-2222-222222222222")
_PREFIX = "ledger:v1:obs:"


async def _seed_account(session, account_id: UUID, user_id: str) -> None:
    session.add(
        ExchangeAccount(
            id=account_id,
            venue="bitfinex",
            label=str(account_id),
            lifecycle_status="active",
        )
    )
    await session.flush()
    await grant_membership(
        session,
        exchange_account_id=account_id,
        user_id=user_id,
        role="owner",
    )


class _Ledger:
    """Scripted ledger ports: one open uncertainty of ``ACCOUNT_ID`` and its fresh evidence."""

    def __init__(self) -> None:
        self.view = UncertaintyView(
            uncertainty_id=UNCERTAINTY_ID, kind="submit_outcome_unknown", symbol="fUST",
            state="open", intended_amount=Decimal("100"), evidence={"cid": 7},
            attempt_id=uuid4(), opened_at_ms=1_100,
        )
        self.observation = uuid4()
        self.candidates: tuple[str, ...] = ()
        self.applied: list[tuple[UUID, str]] = []
        self.apply_error: Exception | None = None

    @property
    def ref(self) -> str:
        return observation_evidence_ref(self.observation)

    def advance(self) -> str:
        """A newer accepted observation arrives: an older reference is now stale."""
        self.observation = uuid4()
        return self.ref

    # OperatorReads
    async def list_uncertainties(self, session, scope, *, state, limit):
        if scope.exchange_account_id != ACCOUNT_ID:
            return ()
        return (self.view,) if state in (None, self.view.state) else ()

    async def get_uncertainty(self, session, scope, uncertainty_id):
        if scope.exchange_account_id != ACCOUNT_ID or uncertainty_id != UNCERTAINTY_ID:
            return None
        return self.view

    # OperatorEvidence
    async def resolution_context(self, session, scope, subject):
        if self.view.state != "open":
            return ResolutionEvidence()
        reason = "multiple_exact_candidates" if len(self.candidates) > 1 else None
        return ResolutionEvidence(
            evidence_ref=self.ref, query_started_at_ms=1_990, query_finished_at_ms=2_000,
            # The ledger port bounds the ids it names and counts them all.
            candidate_venue_offer_ids=self.candidates[:16], candidate_count=len(self.candidates),
            unavailable_reason=reason,
        )

    # OperatorResolution
    def columns(self, evidence_ref: str) -> RequestColumns:
        if not evidence_ref.startswith(_PREFIX):
            raise ResolutionRejected("stale_reconcile_fence")
        try:
            return RequestColumns(observation_id=UUID(evidence_ref.removeprefix(_PREFIX)))
        except ValueError:
            raise ResolutionRejected("stale_reconcile_fence") from None

    def _validate(self, scope, intent) -> RequestColumns:
        if scope.exchange_account_id != ACCOUNT_ID or intent.uncertainty_id != UNCERTAINTY_ID:
            raise ResolutionRejected("not_found", kind="not_found")
        if self.view.state != "open":
            raise ResolutionRejected("uncertainty_already_resolved")
        if intent.evidence_ref != self.ref:
            raise ResolutionRejected("stale_reconcile_fence")
        return self.columns(intent.evidence_ref)

    async def prepare(self, session, scope, intent, *, now_ms):
        return self._validate(scope, intent)

    async def apply(self, session, scope, request, *, now_ms):
        self._validate(scope, request.intent)
        if self.apply_error is not None:
            raise self.apply_error
        self.applied.append((request.request_id, request.intent.action))
        self.view = replace(self.view, state="resolved", resolved_at_ms=now_ms,
                            resolved_by_operator_id=request.intent.operator_id)
        return AppliedResolution()


class _Authority:
    """Records who the worker asked about; ``authorized`` is the verdict."""

    def __init__(self, authorized: set[str] | None = None) -> None:
        self.authorized = {"operator-1"} if authorized is None else authorized
        self.calls: list[tuple[UUID, str]] = []

    async def __call__(self, session, *, account_id: UUID, user: str) -> bool:
        self.calls.append((account_id, user))
        return user in self.authorized


def _worker(factory, ledger: _Ledger, *, account: UUID = ACCOUNT_ID,
            authority: _Authority | None = None):
    from bfx_funding_bot.modules.execution.uncertainty_requests import (
        ResolutionScope,
        UncertaintyResolutionWorker,
    )

    return UncertaintyResolutionWorker(
        session_factory=factory, scope=ResolutionScope(account, "ci"),
        authority=authority or _Authority(), clock=lambda: 5_000, resolution=ledger)


def _apply_queued(app, *, authority: _Authority | None = None) -> bool:
    """Run the daemon-side resolution worker once, as the account writer would."""
    _client, factory, ledger = app
    return asyncio.run(_worker(factory, ledger, authority=authority).tick())


@pytest_asyncio.fixture
async def uncertainty_app(sqlite_engine, monkeypatch):
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as seed:
        await _seed_account(seed, ACCOUNT_ID, "operator-1")
        await _seed_account(seed, OTHER_ACCOUNT_ID, "other-operator")
        await seed.commit()

    from bfx_funding_bot.modules.api.uncertainties import build_uncertainties_router

    ledger = _Ledger()
    app = FastAPI()
    app.include_router(build_uncertainties_router())
    app.state.read_models = ReadModels(ledger, ledger, ledger, object(), object())  # type: ignore[arg-type]

    async def _operator() -> Principal:
        return Principal(user_id="operator-1", email="operator@example.com", role="admin")

    async def _session():
        async with factory() as value:
            try:
                yield value
                await value.commit()
            except Exception:
                await value.rollback()
                raise

    app.dependency_overrides[require_operator] = _operator
    app.dependency_overrides[get_session] = _session
    return TestClient(app), factory, ledger


_BASE = f"/api/v1/exchange-accounts/{ACCOUNT_ID}"


def _mark_not_accepted(client, evidence_ref: str, *, reason: str = "zero candidates"):
    return client.post(
        f"{_BASE}/uncertainties/{UNCERTAINTY_ID}/mark-not-accepted",
        json={
            "evidenceRef": evidence_ref,
            "operatorUuid": "operator-1",
            "reason": reason,
            "evidence": {"candidateCount": 0},
        },
    )


def _request_outcome(client, request_id: str) -> dict[str, object]:
    response = client.get(f"{_BASE}/uncertainty-resolution-requests/{request_id}")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_cross_account_uncertainty_is_non_enumerating_404(uncertainty_app) -> None:
    client, _factory, _ledger = uncertainty_app
    response = client.get(f"/api/v1/exchange-accounts/{OTHER_ACCOUNT_ID}/uncertainties")
    assert response.status_code == 404
    assert response.json()["detail"] == "not_found"
    missing = client.get(f"{_BASE}/uncertainties/{uuid4()}")
    assert (missing.status_code, missing.json()["detail"]) == (404, "not_found")


def test_uncertainty_read_exposes_the_evidence_ports_resolution_context(
    uncertainty_app,
) -> None:
    client, _factory, ledger = uncertainty_app
    ledger.candidates = ("venue-read-context",)
    response = client.get(f"{_BASE}/uncertainties/{UNCERTAINTY_ID}")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["resolutionContext"] == {
        "evidenceRef": ledger.ref,
        "queryStartedAtMs": 1_990,
        "queryFinishedAtMs": 2_000,
        "candidateCount": 1,
        "candidateVenueOfferIds": ["venue-read-context"],
        "unavailableReason": None,
    }
    assert data["blockedScope"] == {
        "exchangeAccountId": str(ACCOUNT_ID), "environment": "ci", "symbol": "fUST"}
    listed = client.get(f"{_BASE}/uncertainties", params={"state": "open"}).json()["data"]
    assert [row["uncertaintyId"] for row in listed] == [str(UNCERTAINTY_ID)]
    assert listed[0]["resolutionContext"] == data["resolutionContext"]


def test_uncertainty_read_renders_the_bounded_candidates_with_the_full_count(
    uncertainty_app,
) -> None:
    client, _factory, ledger = uncertainty_app
    ledger.candidates = tuple(f"venue-{index:02}" for index in range(20))
    context = client.get(f"{_BASE}/uncertainties/{UNCERTAINTY_ID}").json()["data"][
        "resolutionContext"]
    assert context["candidateCount"] == 20
    assert context["candidateVenueOfferIds"] == [f"venue-{index:02}" for index in range(16)]
    assert context["unavailableReason"] == "multiple_exact_candidates"


@pytest.mark.parametrize("applied", [False, True])
def test_uncertainty_and_request_responses_omit_event_seq_fields(
    uncertainty_app, applied: bool,
) -> None:
    client, _factory, ledger = uncertainty_app
    ref = ledger.ref
    queued = _mark_not_accepted(client, ref)
    assert queued.status_code == 202, queued.text
    request_id = queued.json()["data"]["requestId"]
    if applied:
        assert _apply_queued(uncertainty_app) is True

    detail = client.get(f"{_BASE}/uncertainties/{UNCERTAINTY_ID}")
    listed = client.get(f"{_BASE}/uncertainties", params={"state": "resolved" if applied else "open"})
    outcome = client.get(f"{_BASE}/uncertainty-resolution-requests/{request_id}")
    assert detail.status_code == listed.status_code == outcome.status_code == 200
    detail_data = detail.json()["data"]
    assert detail_data["state"] == ("resolved" if applied else "open")
    assert len(listed.json()["data"]) == 1
    request_data = outcome.json()["data"]
    assert request_data["state"] == ("applied" if applied else "requested")
    assert request_data["evidenceRef"] == ref
    forbidden = {
        "openedEventSeq", "reconcileEventSeq", "resolvedEventSeq", "lastEventSeq",
        "opened_event_seq", "reconcile_event_seq", "resolved_event_seq", "last_event_seq",
    }
    for data in [queued.json()["data"], request_data, detail_data, *listed.json()["data"]]:
        assert forbidden.isdisjoint(data)
        if "resolutionContext" in data:
            assert forbidden.isdisjoint(data["resolutionContext"])
            assert data["resolutionContext"]["evidenceRef"] == (None if applied else ref)
        if "resolutionRequest" in data:
            assert forbidden.isdisjoint(data["resolutionRequest"])
            assert data["resolutionRequest"] == request_data


def test_queued_request_is_visible_as_pending_until_the_worker_applies_it(
    uncertainty_app,
) -> None:
    client, _factory, ledger = uncertainty_app
    queued = _mark_not_accepted(client, ledger.ref)
    assert queued.status_code == 202, queued.text
    request_id = queued.json()["data"]["requestId"]

    listed = client.get(f"{_BASE}/uncertainties", params={"state": "open"}).json()["data"]
    assert [row["resolutionRequest"]["requestId"] for row in listed] == [request_id]
    assert listed[0]["resolutionRequest"]["state"] == "requested"
    assert listed[0]["state"] == "open"
    assert ledger.applied == []

    assert _apply_queued(uncertainty_app) is True
    assert _apply_queued(uncertainty_app) is False  # nothing left: one request, one apply
    assert ledger.applied == [(UUID(request_id), "mark_not_accepted")]
    assert client.get(f"{_BASE}/uncertainties", params={"state": "open"}).json()["data"] == []


def test_identical_repeat_is_the_same_request_and_a_different_one_waits(
    uncertainty_app,
) -> None:
    client, _factory, ledger = uncertainty_app
    first = _mark_not_accepted(client, ledger.ref)
    repeat = _mark_not_accepted(client, ledger.ref)
    assert first.status_code == repeat.status_code == 202
    assert repeat.json()["data"]["requestId"] == first.json()["data"]["requestId"]

    different = _mark_not_accepted(client, ledger.ref, reason="another reason")
    assert different.status_code == 409
    assert different.json()["detail"] == "resolution_request_pending"

    assert _apply_queued(uncertainty_app) is True
    after = _mark_not_accepted(client, ledger.ref)
    assert after.status_code == 409
    assert after.json()["detail"] == "uncertainty_already_resolved"
    assert len(ledger.applied) == 1


def test_a_stale_reference_is_refused_when_queueing(uncertainty_app) -> None:
    client, _factory, ledger = uncertainty_app
    stale = ledger.ref
    ledger.advance()
    response = _mark_not_accepted(client, stale)
    assert (response.status_code, response.json()["detail"]) == (409, "stale_reconcile_fence")


@pytest.mark.parametrize("evidence_ref", ["42", "042", " 42", f"{_PREFIX}not-a-uuid"])
def test_a_malformed_evidence_ref_is_refused_before_anything_is_queued(
    uncertainty_app, evidence_ref,
) -> None:
    client, _factory, _ledger = uncertainty_app
    response = client.post(
        f"{_BASE}/uncertainties/{UNCERTAINTY_ID}/mark-not-accepted",
        json={"evidenceRef": evidence_ref},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "stale_reconcile_fence"


def test_worker_records_why_an_accepted_request_no_longer_applies(
    uncertainty_app,
) -> None:
    """Evidence can move between acceptance and apply; say so, never apply."""
    client, _factory, ledger = uncertainty_app
    queued = _mark_not_accepted(client, ledger.ref)
    assert queued.status_code == 202
    ledger.advance()  # a newer fence arrives

    assert _apply_queued(uncertainty_app) is True
    outcome = _request_outcome(client, queued.json()["data"]["requestId"])
    assert outcome["state"] == "rejected"
    assert outcome["outcomeReason"] == "stale_reconcile_fence"
    assert ledger.applied == []
    detail = client.get(f"{_BASE}/uncertainties/{UNCERTAINTY_ID}").json()["data"]
    assert detail["state"] == "open"
    # The console learns why from the list itself, not a separate poll.
    assert detail["resolutionRequest"]["requestId"] == queued.json()["data"]["requestId"]
    assert detail["resolutionRequest"]["state"] == "rejected"
    assert detail["resolutionRequest"]["outcomeReason"] == "stale_reconcile_fence"


def test_worker_failure_is_recorded_with_its_root_cause_not_a_bare_conflict(
    uncertainty_app,
) -> None:
    """The 2026-09-21 permission failure surfaced only as `resolution_rejected`."""

    class InsufficientPrivilegeError(Exception):
        pass

    client, _factory, ledger = uncertainty_app
    queued = _mark_not_accepted(client, ledger.ref)
    try:
        raise InsufficientPrivilegeError("permission denied for table execution_resolution_journal")
    except InsufficientPrivilegeError as cause:
        error = RuntimeError("journal write failed")
        error.__cause__ = cause
    ledger.apply_error = error

    assert _apply_queued(uncertainty_app) is True
    outcome = _request_outcome(client, queued.json()["data"]["requestId"])
    assert outcome["state"] == "failed"
    assert outcome["outcomeReason"] == "resolution_failed:InsufficientPrivilegeError"
    assert ledger.applied == []

    # A failed request is terminal; the operator may ask again.
    ledger.apply_error = None
    retry = _mark_not_accepted(client, ledger.ref)
    assert retry.status_code == 202, retry.text
    assert retry.json()["data"]["requestId"] != queued.json()["data"]["requestId"]
    assert _apply_queued(uncertainty_app) is True
    assert _request_outcome(client, retry.json()["data"]["requestId"])["state"] == "applied"


def test_only_the_configured_operator_can_queue_an_adjudication() -> None:
    """The web API admits exactly who the worker will authorize.

    `require_operator` (configured operator id + admin role) guards every
    adjudication route through `require_account_member`, so a writer who is not
    the operator is refused with 403 before anything is queued -- never accepted
    and later rejected as `operator_not_authorized`. The worker's database check then
    only catches what changed since: ban, lost TOTP, lost membership.
    """
    from fastapi.dependencies.models import Dependant
    from fastapi.routing import APIRoute

    from bfx_funding_bot.modules.api.uncertainties import build_uncertainties_router

    def calls(dependant: Dependant) -> set[object]:
        found = {dependant.call}
        for child in dependant.dependencies:
            found |= calls(child)
        return found

    posts = [
        route for route in build_uncertainties_router().routes
        if isinstance(route, APIRoute) and "POST" in route.methods
    ]
    assert {route.path.rsplit("/", 1)[-1] for route in posts} == {
        "bind-to-venue", "mark-not-accepted", "manual-resolution",
    }
    for route in posts:
        assert require_operator in calls(route.dependant), route.path


def test_resolution_request_reads_are_account_scoped(uncertainty_app) -> None:
    client, _factory, ledger = uncertainty_app
    request_id = _mark_not_accepted(client, ledger.ref).json()["data"]["requestId"]
    denied = client.get(
        f"/api/v1/exchange-accounts/{OTHER_ACCOUNT_ID}/uncertainty-resolution-requests/{request_id}"
    )
    assert denied.status_code == 404
    missing = client.get(f"{_BASE}/uncertainty-resolution-requests/{uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "not_found"


async def _queue_directly(factory, *, uncertainty_id: UUID, created_at_ms: int) -> UUID:
    """A request the web API would have refused; the worker must still settle it."""
    from sqlalchemy import insert

    from bfx_funding_bot.modules.execution.uncertainty_tables import (
        UncertaintyResolutionRequestRow,
    )

    request_id = uuid4()
    async with factory.begin() as session:
        await session.execute(insert(UncertaintyResolutionRequestRow).values(
            request_id=request_id, exchange_account_id=ACCOUNT_ID, deployment_environment="ci",
            uncertainty_id=uncertainty_id, action="mark_not_accepted", observation_id=uuid4(),
            requested_by="operator-1", created_at_ms=created_at_ms,
        ))
    return request_id


def _unrecordable(monkeypatch, request_id: str) -> None:
    """The worker's apply of ``request_id`` yields an outcome its row cannot hold."""
    from bfx_funding_bot.modules.execution.operator_requests import APPLIED, Outcome
    from bfx_funding_bot.modules.execution.uncertainty_requests import (
        UncertaintyResolutionWorker,
    )

    original = UncertaintyResolutionWorker.apply

    async def apply(self, session, row, prepared):
        if str(row.request_id) == request_id:
            # An applied resolution carries no reason (ck_..._outcome_shape): the flush fails.
            return Outcome(APPLIED, "unrecordable")
        return await original(self, session, row, prepared)

    monkeypatch.setattr(UncertaintyResolutionWorker, "apply", apply)


def test_a_request_whose_outcome_cannot_be_written_does_not_block_the_queue(
    uncertainty_app, monkeypatch
) -> None:
    """Head-of-line poison: the outcome write fails, so the tick used to roll back
    and the same oldest row was picked again forever, starving every later one."""
    client, factory, ledger = uncertainty_app
    poison = _mark_not_accepted(client, ledger.ref).json()["data"]["requestId"]
    later = asyncio.run(_queue_directly(factory, uncertainty_id=uuid4(), created_at_ms=10**15))
    _unrecordable(monkeypatch, poison)

    assert _apply_queued(uncertainty_app) is True
    first = _request_outcome(client, poison)
    assert first["state"] == "failed"
    assert str(first["outcomeReason"]).startswith("outcome_write_failed:")

    assert _apply_queued(uncertainty_app) is True
    assert _request_outcome(client, str(later))["state"] == "rejected"
    assert _apply_queued(uncertainty_app) is False


def test_worker_skips_a_request_it_cannot_even_mark_failed(uncertainty_app, monkeypatch) -> None:
    """If the fallback write also fails, the worker moves on rather than spin."""
    from bfx_funding_bot.modules.execution.uncertainty_requests import (
        UncertaintyResolutionWorker,
    )

    client, factory, ledger = uncertainty_app
    poison = _mark_not_accepted(client, ledger.ref).json()["data"]["requestId"]
    later = asyncio.run(_queue_directly(factory, uncertainty_id=uuid4(), created_at_ms=10**15))
    _unrecordable(monkeypatch, poison)

    async def cannot_mark(self, request_id, reason):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(UncertaintyResolutionWorker, "_mark_failed", cannot_mark)
    worker = _worker(factory, ledger)

    async def two_ticks() -> tuple[bool, bool]:
        return await worker.tick(), await worker.tick()

    assert asyncio.run(two_ticks()) == (True, True)
    assert _request_outcome(client, poison)["state"] == "requested"  # left for a restart
    assert _request_outcome(client, str(later))["state"] == "rejected"


def test_worker_only_touches_its_own_account(uncertainty_app) -> None:
    client, factory, ledger = uncertainty_app
    request_id = _mark_not_accepted(client, ledger.ref).json()["data"]["requestId"]
    other = _worker(factory, ledger, account=OTHER_ACCOUNT_ID)
    assert asyncio.run(other.tick()) is False
    assert _request_outcome(client, request_id)["state"] == "requested"


def test_revoked_operator_request_is_rejected_without_an_apply(uncertainty_app) -> None:
    """Authority is re-read when the daemon applies, not trusted from acceptance."""
    client, _factory, ledger = uncertainty_app
    queued = _mark_not_accepted(client, ledger.ref)
    assert queued.status_code == 202, queued.text

    revoked = _Authority(authorized=set())
    assert _apply_queued(uncertainty_app, authority=revoked) is True
    assert revoked.calls == [(ACCOUNT_ID, "operator-1")]
    outcome = _request_outcome(client, queued.json()["data"]["requestId"])
    assert outcome["state"] == "rejected"
    assert outcome["outcomeReason"] == "operator_not_authorized"
    assert ledger.applied == []
    detail = client.get(f"{_BASE}/uncertainties/{UNCERTAINTY_ID}").json()["data"]
    assert detail["state"] == "open"


def test_authorized_operator_request_is_applied_after_the_authority_check(
    uncertainty_app,
) -> None:
    client, _factory, ledger = uncertainty_app
    queued = _mark_not_accepted(client, ledger.ref)

    authority = _Authority()
    assert _apply_queued(uncertainty_app, authority=authority) is True
    assert authority.calls == [(ACCOUNT_ID, "operator-1")]
    outcome = _request_outcome(client, queued.json()["data"]["requestId"])
    assert outcome["state"] == "applied"
    assert len(ledger.applied) == 1


@pytest.mark.parametrize(
    ("configured", "role", "user"),
    [
        ("operator-1", "admin", "someone-else"),
        ("operator-1", "viewer", "operator-1"),
        ("", "admin", ""),
    ],
)
def test_shared_operator_authority_refuses_before_asking_the_database(
    monkeypatch, configured: str, role: str, user: str
) -> None:
    """Configured sole admin first; the database function is never reached."""
    from unittest.mock import AsyncMock

    from bfx_funding_bot.modules.execution.operator_requests import operator_authorized

    monkeypatch.delenv("BFX_PHASE", raising=False)
    monkeypatch.setenv("BFX_OPERATOR_USER_ID", configured)
    monkeypatch.setenv("BFX_OPERATOR_ROLE", role)
    session = AsyncMock()
    assert asyncio.run(operator_authorized(session, account_id=ACCOUNT_ID, user=user)) is False
    session.scalar.assert_not_called()


def test_shared_operator_authority_defers_to_the_database_verdict(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from bfx_funding_bot.modules.execution.operator_requests import operator_authorized

    monkeypatch.delenv("BFX_PHASE", raising=False)
    monkeypatch.setenv("BFX_OPERATOR_USER_ID", "operator-1")
    monkeypatch.setenv("BFX_OPERATOR_ROLE", "admin")
    for verdict in (True, False):
        session = AsyncMock()
        session.scalar.return_value = verdict
        assert asyncio.run(
            operator_authorized(session, account_id=ACCOUNT_ID, user="operator-1")
        ) is verdict
        _statement, params = session.scalar.call_args.args
        assert params == {"account": ACCOUNT_ID, "actor": "operator-1"}


@pytest.mark.parametrize("field", ["reconcile_event_seq", "reconcileEventSeq"])
def test_old_resolution_body_field_is_rejected(uncertainty_app, field) -> None:
    client, _factory, _ledger = uncertainty_app
    response = client.post(
        f"{_BASE}/uncertainties/{UNCERTAINTY_ID}/mark-not-accepted", json={field: 42},
    )
    assert response.status_code == 422
    assert any(error["type"] == "extra_forbidden" for error in response.json()["detail"])


def test_resolution_evidence_rejects_unknown_secret_like_fields(uncertainty_app) -> None:
    client, _factory, ledger = uncertainty_app
    response = client.post(
        f"{_BASE}/uncertainties/{UNCERTAINTY_ID}/mark-not-accepted",
        json={
            "evidenceRef": ledger.ref,
            "evidence": {"candidateCount": 0, "apiSecret": "must-not-persist"},
        },
    )
    assert response.status_code == 422


def test_manual_resolution_requires_reason_and_operator_uuid(uncertainty_app) -> None:
    client, _factory, ledger = uncertainty_app
    response = client.post(
        f"{_BASE}/uncertainties/{uuid4()}/manual-resolution",
        json={"evidenceRef": ledger.ref, "operatorUuid": "operator-1"},
    )
    assert response.status_code == 422


def test_request_response_echoes_stored_ref_after_context_advances(uncertainty_app) -> None:
    client, _factory, ledger = uncertainty_app
    ref = ledger.ref
    queued = _mark_not_accepted(client, ref).json()["data"]
    assert queued["evidenceRef"] == ref
    newer = ledger.advance()
    detail = client.get(f"{_BASE}/uncertainties/{UNCERTAINTY_ID}").json()["data"]
    assert detail["resolutionContext"]["evidenceRef"] == newer
    assert detail["resolutionRequest"]["evidenceRef"] == ref
    assert _request_outcome(client, queued["requestId"])["evidenceRef"] == ref
