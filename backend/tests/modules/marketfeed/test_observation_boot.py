"""Daemon boot uses the scoped cycle port, under one rule for either authority.

* ``query_admission_refused`` raises ``BootInvariantError``: at grace 0 under the writer lock
  every outcome-less attempt was closed first, so a refusal cannot happen.
* ``fenced`` / ``incomplete_or_unequal`` boot with a warning and seed the periodic loop's
  non-accepted streak; trading stays blocked by the missing accepted basis.
* ``accepted`` boots. The legacy sink never answers anything else (it raises to refuse).

Mutation checks (one at a time; revert after each):

* boot continues on ``query_admission_refused``: ``test_admission_refusal_refuses_the_boot``.
* boot raises on ``fenced`` / ``incomplete_or_unequal``: ``test_a_non_accepted_boot_cycle_*``.
* the streak is not seeded, or an accepted boot seeds it: ``test_a_non_accepted_boot_cycle_*``,
  ``test_daemon_boot_passes_scope_to_observation_port``.
* a refused boot loses the protection's pending stop: ``test_admission_refusal_refuses_the_boot``.
"""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from bfx_funding_bot.core.errors import BootInvariantError
from bfx_funding_bot.modules.ledger import CycleResult, Scope
from bfx_funding_bot.modules.marketfeed.daemon import Daemon


def _daemon(decision: str, *, protection=None, periodic=None):
    scope = Scope(uuid4(), "ci")
    sink = AsyncMock()
    sink.run.return_value = CycleResult(decision, uuid4() if decision == "accepted" else None)
    return scope, sink, SimpleNamespace(
        boot_recovery=sink, observation_scope=scope, protection=protection,
        periodic_reconcile=periodic,
    )


@pytest.mark.asyncio
async def test_daemon_boot_passes_scope_to_observation_port():
    periodic = MagicMock()
    scope, sink, daemon = _daemon("accepted", periodic=periodic)
    await Daemon._run_boot_recovery(daemon)
    sink.run.assert_awaited_once_with(scope)
    periodic.note_boot.assert_not_called()


@pytest.mark.asyncio
async def test_admission_refusal_refuses_the_boot():
    protection = SimpleNamespace(run_pending=AsyncMock())
    periodic = MagicMock()
    _, _, daemon = _daemon("query_admission_refused", protection=protection, periodic=periodic)
    with pytest.raises(BootInvariantError, match="query admission"):
        await Daemon._run_boot_recovery(daemon)
    protection.run_pending.assert_awaited_once()
    periodic.note_boot.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["fenced", "incomplete_or_unequal"])
async def test_a_non_accepted_boot_cycle_boots_and_seeds_the_streak(decision, caplog):
    periodic = MagicMock()
    protection = SimpleNamespace(run_pending=AsyncMock())
    _, sink, daemon = _daemon(decision, protection=protection, periodic=periodic)
    with caplog.at_level(logging.WARNING):
        await Daemon._run_boot_recovery(daemon)
    sink.run.assert_awaited_once()
    periodic.note_boot.assert_called_once_with(decision)
    protection.run_pending.assert_not_awaited()
    assert f"boot_observation_not_accepted decision={decision}" in caplog.text


@pytest.mark.asyncio
async def test_a_process_without_periodic_reconcile_still_boots_on_a_non_accepted_cycle():
    _, _, daemon = _daemon("fenced", periodic=None)
    await Daemon._run_boot_recovery(daemon)
