"""The two prod UNKNOWN sequences, end to end through a bot process of each capital authority.

One synthetic scenario per sequence (labels only: "seq A 9502->9539", "seq B 9849->9859"; the
amounts are synthetic): a submit whose POST answered 5xx on fUST, with no matching offer at
the venue, so the UNKNOWN resolves NOT_ACCEPTED. Everything runs through
``bot.build_daemon`` -> ``_run_boot_recovery`` -> ``periodic_reconcile._tick`` ->
``command_gate.submit`` with the venue faked at HTTP level and one composition clock, once per
authority (legacy, ledger) and once per resolution path:

* ``automatic``: seq A is resolved by a periodic tick past the settle window, seq B by a restart
  (the boot sink, grace 0);
* ``operator``: the resolution is queued as an operator request and applied by the bot's own
  uncertainty worker (the historical prod path), inside the settle window.

Both authorities assert the same neutral observables; what differs is data in
``EXPECTED_DIVERGENCE``. Mutation map (apply one at a time, run this file, revert) is in the
S1-3e6 report; each assertion below names what it guards.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from bfx_funding_bot.modules.execution.command_gate import (
    CommandGateBlocked,
    SubmitOutcomeLostError,
)
from bfx_funding_bot.modules.ledger import ResolutionIntent, UnknownResolutionNotice
from bfx_funding_bot.modules.observability import alerts

from .bot_e2e import (
    T0,
    AllowOperator,
    BotEnv,
    DyingVenue,
    accepting,
    bot_env,  # noqa: F401 - fixture
    ledger_db,  # noqa: F401 - fixture dependency
    lost_response,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

OPERATOR = "operator-e2e"
SETTLE_MS = 120_000
SYSTEM_RESOLVER = "system:reconcile"
AUTOMATIC_REASON = "fingerprint_absent_from_complete_history"
UNKNOWN_KIND = "submit_outcome_unknown"
NEVER_ALERTED = (
    alerts.PROTECTION_TRIPPED, alerts.FOREIGN_EXPOSURE, alerts.RECONCILE_NOT_ACCEPTED,
    "foreign_lending",
)


@pytest.fixture(params=["legacy", "ledger"])
def authority(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(params=["automatic", "operator"])
def resolver(request: pytest.FixtureRequest) -> str:
    return request.param


@dataclass(frozen=True)
class UnknownSequence:
    label: str
    amount: Decimal
    submitted_at: int  # when the submit starts on the composition clock


SEQ_A = UnknownSequence("seq A 9502->9539", Decimal("200.00000501"), T0 + 10_000)
SEQ_B = UnknownSequence("seq B 9849->9859", Decimal("200.00000502"), T0 + 500_000)


@dataclass(frozen=True)
class Divergence:
    """What one authority does differently, declared once."""

    # ``UnknownResolutionNotice`` per UNKNOWN the observation cycle resolved by itself: the
    # ledger publishes it, the legacy path has no such event.
    notices_per_automatic_resolution: int
    # A cycle whose history is incomplete: the legacy sink accepts the snapshot and withholds
    # only the UNKNOWN's symbol; the ledger refuses acceptance account-wide.
    incomplete_cycle_non_accepted: int
    incomplete_cycle_capital_reason: str
    # The resolution's timestamp: the ledger stamps it from the composition clock; the legacy
    # read model shows the event store's database ``recorded_at`` (server wall time).
    resolved_at_follows_clock: bool
    # A submit that died before its outcome, met by a boot young enough to be inside the
    # runtime grace: the ledger boot sink (grace 0, writer lock held) closes it UNKNOWN at
    # once; ``BootRecovery`` turns a PENDING claim UNKNOWN only after its own 120 s
    # (``grace_ms``), which the boot sink's ``action_grace_ms`` does not shorten.
    young_dangling_is_unknown_at_boot: bool


EXPECTED_DIVERGENCE = {
    "legacy": Divergence(0, 0, "execution_unknown", False, False),
    "ledger": Divergence(1, 1, "snapshot_query_pending", True, True),
}


class Notices:
    def __init__(self, daemon: Any) -> None:
        self.seen: list[UnknownResolutionNotice] = []
        daemon.bus.subscribe(UnknownResolutionNotice, self._on)

    async def _on(self, event: UnknownResolutionNotice) -> None:
        self.seen.append(event)


async def settle_clean(env: BotEnv, daemon: Any, at: int, *, resolved: int, opened: int) -> None:
    """One periodic tick; the cycle is accepted and nothing but the UNKNOWN is touched."""
    before = len(env.deployments(daemon))
    await env.tick(daemon, at)
    assert daemon.periodic_reconcile._non_accepted == 0  # cycle accepted (mutation 12)
    assert len(env.deployments(daemon)) == before + 1  # deployed once, on the accepted cycle
    assert not env.deployments(daemon)[-1]  # no venue offers to deploy over
    assert len(await env.uncertainties("resolved")) == resolved
    assert len(await env.uncertainties("open")) == opened
    assert not [item for item in env.alerts if item in NEVER_ALERTED]


async def submit_lost(env: BotEnv, daemon: Any, seq: UnknownSequence) -> None:
    env.clock.now = seq.submitted_at
    ready = await env.ready(daemon, seq.amount)
    fusd = await env.status(daemon, "fUSD")
    result = await env.submit(daemon, ready, lost_response())
    assert result.outcome_kind.value == "unknown", seq.label
    assert await env.has_open(daemon)
    status = await env.status(daemon, "fUST")
    assert (status["capital_available"], status["reason"]) == (False, "execution_unknown")
    assert await env.status(daemon, "fUSD") == fusd  # another symbol is unaffected
    probe = lost_response()
    with pytest.raises(CommandGateBlocked):  # nothing reaches the venue while it is open
        await env.submit(daemon, ready, probe)
    assert probe.calls == 0


async def assert_freed(env: BotEnv, daemon: Any, total_capital: str) -> None:
    status = await env.status(daemon, "fUST")
    assert status["capital_available"] is True, status
    assert status["unreflected_commitments"] == "0"
    assert status["total_capital"] == total_capital
    assert not await env.has_open(daemon)


async def operator_resolves(env: BotEnv, daemon: Any, at: int) -> None:
    (view,) = await env.uncertainties("open")
    env.clock.now = at
    ref = await env.evidence_ref(view)
    worker = daemon.uncertainty_worker
    worker.authority = AllowOperator()
    async with env.factory.begin() as session:
        queued = await worker.requests.request(
            session, ResolutionIntent(view.uncertainty_id, "mark_not_accepted", ref, OPERATOR,
                                      reason="absent"), now_ms=at)
    assert queued.state == "requested"
    assert await worker.tick() is True
    async with env.factory() as session:
        done = await worker.requests.get(session, queued.request_id)
    assert (done.state, done.outcome_reason) == ("applied", None)


async def assert_resolved_inside(env: BotEnv, divergence: Divergence, since: int) -> None:
    """The newest resolution was written by the step that started at ``since``."""
    if divergence.resolved_at_follows_clock:
        newest = (await env.uncertainties("resolved"))[0]
        assert since <= (newest.resolved_at_ms or 0) <= env.clock()


def assert_resolved_by(view: Any, resolver: str) -> None:
    assert view.kind == UNKNOWN_KIND
    if resolver == "automatic":
        assert (view.resolved_by_operator_id, view.resolution_reason) == (
            SYSTEM_RESOLVER, AUTOMATIC_REASON)
    else:
        assert (view.resolved_by_operator_id, view.resolution_reason) == (OPERATOR, "absent")


async def test_two_prod_unknown_sequences_resolve_not_accepted_in_a_row(
    bot_env: BotEnv, authority: str, resolver: str,  # noqa: F811
) -> None:
    env, divergence = bot_env, EXPECTED_DIVERGENCE[authority]
    daemon = await env.build()
    notices = Notices(daemon)
    await env.boot(daemon, T0)
    assert daemon.periodic_reconcile._non_accepted == 0
    total = (await env.status(daemon, "fUST"))["total_capital"]

    # ---- seq A: resolved by a periodic tick, or by an operator request
    await submit_lost(env, daemon, SEQ_A)
    await settle_clean(env, daemon, SEQ_A.submitted_at + 60_000, resolved=0, opened=1)
    assert (await env.status(daemon, "fUST"))["reason"] == "execution_unknown"  # stays open
    if resolver == "automatic":
        # A query that starts 300 ms before the settle window ends and finishes after it
        # proves nothing yet: the window is judged at the query's start (mutation 4).
        await settle_clean(env, daemon, SEQ_A.submitted_at + SETTLE_MS - 300, resolved=0, opened=1)
        assert (await env.status(daemon, "fUST"))["reason"] == "execution_unknown"
        resolving = SEQ_A.submitted_at + SETTLE_MS + 10_000
        await settle_clean(env, daemon, resolving, resolved=1, opened=0)
        await assert_resolved_inside(env, divergence, resolving)
        # the resolving tick's basis was written before the resolution: next tick frees
        assert (await env.status(daemon, "fUST"))["reason"] == "execution_unknown"
        await settle_clean(env, daemon, SEQ_A.submitted_at + 220_000, resolved=1, opened=0)
    else:
        await operator_resolves(env, daemon, SEQ_A.submitted_at + 70_000)
        await assert_resolved_inside(env, divergence, SEQ_A.submitted_at + 70_000)
        await settle_clean(env, daemon, SEQ_A.submitted_at + 90_000, resolved=1, opened=0)
    await assert_freed(env, daemon, total)
    (view_a,) = await env.uncertainties("resolved")
    assert_resolved_by(view_a, resolver)
    assert len(notices.seen) == (
        divergence.notices_per_automatic_resolution if resolver == "automatic" else 0)

    # ---- seq B: resolved by a restart (boot sink, grace 0), or by an operator request
    await settle_clean(env, daemon, SEQ_B.submitted_at - 5_000, resolved=1, opened=0)
    await submit_lost(env, daemon, SEQ_B)
    await settle_clean(env, daemon, SEQ_B.submitted_at + 60_000, resolved=1, opened=1)
    daemon = await env.restart(daemon)
    notices_b = Notices(daemon)
    if resolver == "automatic":
        resolving = SEQ_B.submitted_at + SETTLE_MS + 10_000
        await env.boot(daemon, resolving)
        await assert_resolved_inside(env, divergence, resolving)
        assert not await env.has_open(daemon)
        assert len(await env.uncertainties("resolved")) == 2
    else:
        await env.boot(daemon, SEQ_B.submitted_at + 80_000)
        assert await env.has_open(daemon)
        await operator_resolves(env, daemon, SEQ_B.submitted_at + 90_000)
        await assert_resolved_inside(env, divergence, SEQ_B.submitted_at + 90_000)
    assert daemon.periodic_reconcile._non_accepted == 0
    await settle_clean(env, daemon, SEQ_B.submitted_at + 230_000, resolved=2, opened=0)
    await assert_freed(env, daemon, total)
    newest, older = await env.uncertainties("resolved")
    assert_resolved_by(newest, resolver)
    assert older == view_a  # the first sequence's record is untouched by the second
    assert len(notices_b.seen) == (
        divergence.notices_per_automatic_resolution if resolver == "automatic" else 0)

    # ---- an extra tick resolves nothing more, and the gate takes a submit again
    await settle_clean(env, daemon, SEQ_B.submitted_at + 300_000, resolved=2, opened=0)
    assert len(notices_b.seen) == (
        divergence.notices_per_automatic_resolution if resolver == "automatic" else 0)
    assert await env.resolution_records() == 2  # one stored resolution per UNKNOWN, no more
    ready = await env.ready(daemon, Decimal("200.00000503"))
    venue = accepting()
    await env.submit(daemon, ready, venue)
    assert venue.calls == 1


async def test_an_unknown_on_a_symbol_without_a_funding_wallet_resolves_not_accepted(
    bot_env: BotEnv, authority: str,  # noqa: F811
) -> None:
    """G1 parity: the UNKNOWN's symbol has no funding wallet when it resolves; both stacks
    still read its history (legacy account-wide, ledger through the attempt's anchor symbol)."""
    env = bot_env
    daemon = await env.build()
    await env.boot(daemon, T0)
    await submit_lost(env, daemon, SEQ_A)
    env.venue.wallets = [["funding", "USD", "500", 0, "500"]]  # no fUST wallet any more

    await env.tick(daemon, SEQ_A.submitted_at + 60_000)
    assert await env.has_open(daemon)
    await env.tick(daemon, SEQ_A.submitted_at + SETTLE_MS + 10_000)

    assert not await env.has_open(daemon)
    (view,) = await env.uncertainties("resolved")
    assert_resolved_by(view, "automatic")
    assert any(path == "funding/offers/fUST/hist" or path == "funding/offers/hist"
               for path in env.venue.paths)


async def test_incomplete_history_at_the_resolving_cycle_is_a_declared_divergence(
    bot_env: BotEnv, authority: str,  # noqa: F811
) -> None:
    env, divergence = bot_env, EXPECTED_DIVERGENCE[authority]
    daemon = await env.build()
    await env.boot(daemon, T0)
    await submit_lost(env, daemon, SEQ_A)
    resolving = SEQ_A.submitted_at + SETTLE_MS + 10_000

    env.venue.history_fails = True
    await env.tick(daemon, resolving)
    # Past the settle window but without a complete history: the UNKNOWN stays open on both.
    assert daemon.periodic_reconcile._non_accepted == divergence.incomplete_cycle_non_accepted
    assert await env.has_open(daemon)
    assert not await env.uncertainties("resolved")
    status = await env.status(daemon, "fUST")
    assert (status["capital_available"], status["reason"]) == (
        False, divergence.incomplete_cycle_capital_reason)

    env.venue.history_fails = False
    await settle_clean(env, daemon, resolving + 10_000, resolved=1, opened=0)  # converges
    assert daemon.periodic_reconcile._non_accepted == 0
    await settle_clean(env, daemon, resolving + 100_000, resolved=1, opened=0)
    assert (await env.status(daemon, "fUST"))["capital_available"] is True
    assert_resolved_by((await env.uncertainties("resolved"))[0], "automatic")


async def test_a_submit_cut_off_before_its_outcome_is_unknown_at_boot_then_resolves(
    bot_env: BotEnv, authority: str,  # noqa: F811
) -> None:
    """The process dies with the submit in flight: the next boot (grace 0, it holds the writer
    lock) closes the attempt UNKNOWN, and the settle window then resolves it NOT_ACCEPTED."""
    env, divergence = bot_env, EXPECTED_DIVERGENCE[authority]
    daemon = await env.build()
    await env.boot(daemon, T0)
    env.clock.now = SEQ_A.submitted_at
    ready = await env.ready(daemon, SEQ_A.amount)
    with pytest.raises(SubmitOutcomeLostError):  # the gate ends the process (DAEMON_FATAL)
        await env.submit(daemon, ready, DyingVenue(lambda _: None))
    assert not await env.uncertainties("open")  # nothing says UNKNOWN yet

    daemon = await env.restart(daemon)
    await env.boot(daemon, SEQ_A.submitted_at + 10_000)  # young: only grace 0 closes it
    assert await env.has_open(daemon) is divergence.young_dangling_is_unknown_at_boot
    assert not await env.uncertainties("resolved")
    assert daemon.periodic_reconcile._non_accepted == 0

    # Past both windows the attempt is UNKNOWN on either stack (legacy: in this very cycle or
    # the next) and the settle window resolves it.
    await env.tick(daemon, SEQ_A.submitted_at + SETTLE_MS + 5_000)
    await settle_clean(env, daemon, SEQ_A.submitted_at + SETTLE_MS + 10_000, resolved=1, opened=0)
    assert_resolved_by((await env.uncertainties("resolved"))[0], "automatic")
    await settle_clean(env, daemon, SEQ_A.submitted_at + 220_000, resolved=1, opened=0)
    assert (await env.status(daemon, "fUST"))["capital_available"] is True
