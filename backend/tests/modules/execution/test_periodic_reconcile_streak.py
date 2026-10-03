"""PeriodicReconcile under either authority: deploy input and the non-accepted streak.

Mutations (apply one at a time, run this file, revert):

1. deploy on a non-accepted cycle: ``test_a_non_accepted_cycle_never_deploys``.
2. alert at streak 2 / never: ``test_alert_fires_once_when_the_streak_reaches_three``.
3. an exception resets the streak: ``test_an_exception_neither_resets_nor_counts_the_streak``.
8. legacy input drops the unmanaged filter: ``test_legacy_input_is_todays_filter``.
9. an accepted cycle does not reset the streak: ``test_an_accepted_cycle_resets_the_streak``.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.core.telemetry import HealthStatus, HealthTarget
from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.modules.execution import periodic_reconcile
from bfx_funding_bot.modules.execution.boot_recovery import ReconcileResult
from bfx_funding_bot.modules.execution.deployment_input import LegacyDeploymentInput
from bfx_funding_bot.modules.execution.observation_sink import LegacyCycleResult
from bfx_funding_bot.modules.execution.periodic_reconcile import PeriodicReconcile
from bfx_funding_bot.modules.ledger import CycleResult, Scope
from bfx_funding_bot.modules.observability import alerts

FENCED = CycleResult("fenced")
ACCEPTED = CycleResult("accepted")


class _Probe:
    def __init__(self) -> None:
        self.updates: list[tuple] = []

    def record_heartbeat(self, sub_task: str) -> None: ...

    def update(self, target, status, **fields) -> None:
        self.updates.append((target, status))


class _Sink:
    def __init__(self, *items) -> None:
        self.items = list(items)

    async def run(self, scope):
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _Input:
    def __init__(self, offers=()) -> None:
        self.offers_ = offers
        self.cycles: list = []

    async def offers(self, cycle):
        self.cycles.append(cycle)
        return self.offers_


class _Deployment:
    def __init__(self) -> None:
        self.received: list[tuple] = []

    async def deploy(self, *, venue_offers=()) -> None:
        self.received.append(venue_offers)


@pytest.fixture
def sent(monkeypatch) -> list[tuple]:
    out: list[tuple] = []
    monkeypatch.setattr(
        periodic_reconcile.alerts, "emit", lambda event, **fields: out.append((event, fields))
    )
    return out


def _periodic(sink, *, probe=None, deployment=None, deployment_input=None):
    return PeriodicReconcile(
        recovery=sink, scope=Scope(uuid4(), "ci"), probe=probe or _Probe(), interval_s=90,
        deployment=deployment, deployment_input=deployment_input,
    )


async def _ticks(periodic, n: int) -> None:
    for _ in range(n):
        await periodic._tick()


def _health(probe: _Probe) -> list[HealthStatus]:
    return [status for target, status in probe.updates if target == HealthTarget.RECONCILE]


@pytest.mark.asyncio
async def test_a_non_accepted_cycle_never_deploys(sent) -> None:
    deployment, source = _Deployment(), _Input()
    periodic = _periodic(_Sink(FENCED, ACCEPTED), deployment=deployment, deployment_input=source)
    await _ticks(periodic, 1)
    assert deployment.received == [] and source.cycles == []
    await _ticks(periodic, 1)
    assert len(deployment.received) == 1


@pytest.mark.asyncio
async def test_an_accepted_ledger_cycle_deploys_from_the_input_and_reports_no_drift(sent) -> None:
    offer = ActiveFundingOffer("7", "fUST", Decimal("200"), 0.001, 2, 0, "active")
    deployment, source, probe = _Deployment(), _Input((offer,)), _Probe()
    periodic = _periodic(
        _Sink(ACCEPTED), probe=probe, deployment=deployment, deployment_input=source
    )
    await _ticks(periodic, 1)
    assert source.cycles == [ACCEPTED]
    assert deployment.received == [(offer,)]
    assert probe.updates == []


@pytest.mark.asyncio
async def test_alert_fires_once_when_the_streak_reaches_three(sent) -> None:
    probe = _Probe()
    periodic = _periodic(_Sink(*([FENCED] * 5)), probe=probe)
    await _ticks(periodic, 2)
    assert sent == []
    await _ticks(periodic, 1)
    assert [event for event, _ in sent] == [alerts.RECONCILE_NOT_ACCEPTED]
    await _ticks(periodic, 2)
    assert len(sent) == 1
    assert _health(probe) == [HealthStatus.DEGRADED] * 5


@pytest.mark.asyncio
async def test_an_accepted_cycle_resets_the_streak(sent) -> None:
    probe = _Probe()
    periodic = _periodic(_Sink(FENCED, FENCED, ACCEPTED, FENCED, FENCED), probe=probe)
    await _ticks(periodic, 5)
    assert sent == []
    assert _health(probe) == [
        HealthStatus.DEGRADED, HealthStatus.DEGRADED, HealthStatus.HEALTHY,
        HealthStatus.DEGRADED, HealthStatus.DEGRADED,
    ]


@pytest.mark.asyncio
async def test_an_exception_neither_resets_nor_counts_the_streak(sent) -> None:
    probe = _Probe()
    periodic = _periodic(_Sink(FENCED, FENCED, RuntimeError("venue down"), FENCED), probe=probe)
    await _ticks(periodic, 3)
    assert sent == []
    assert _health(probe) == [HealthStatus.DEGRADED] * 2   # the failure is EXECUTOR's alone
    await _ticks(periodic, 1)
    assert [event for event, _ in sent] == [alerts.RECONCILE_NOT_ACCEPTED]


@pytest.mark.asyncio
async def test_note_boot_seeds_the_streak(sent) -> None:
    periodic = _periodic(_Sink(FENCED, FENCED))
    periodic.note_boot("incomplete_or_unequal")
    await _ticks(periodic, 1)
    assert sent == []
    await _ticks(periodic, 1)
    assert [event for event, _ in sent] == [alerts.RECONCILE_NOT_ACCEPTED]


@pytest.mark.asyncio
async def test_an_accepted_boot_leaves_no_streak(sent) -> None:
    probe = _Probe()
    periodic = _periodic(_Sink(), probe=probe)
    periodic.note_boot("accepted")
    assert probe.updates == [] and sent == []


@pytest.mark.asyncio
async def test_legacy_input_is_todays_filter() -> None:
    def offer(venue_offer_id: str) -> ActiveFundingOffer:
        return ActiveFundingOffer(venue_offer_id, "fUST", Decimal("200"), 0.001, 2, 0, "ACTIVE")

    managed, foreign = offer("42"), offer("777")
    result = ReconcileResult(
        0, 0, 0, venue_offers=(managed, foreign), unmanaged_offer_ids=frozenset({"777"})
    )
    cycle = LegacyCycleResult("accepted", legacy=result)
    assert await LegacyDeploymentInput().offers(cycle) == (managed,)
    with pytest.raises(TypeError):
        await LegacyDeploymentInput().offers(ACCEPTED)


def test_deployment_and_its_input_come_together() -> None:
    with pytest.raises(ValueError, match="together"):
        _periodic(_Sink(), deployment=_Deployment())
    with pytest.raises(ValueError, match="together"):
        _periodic(_Sink(), deployment_input=_Input())
