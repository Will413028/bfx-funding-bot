"""A spent canary permit must not be a halt with no exit.

The canary permit is bound to the halt epoch and UNIQUE on it, and it is spent
the moment the command is issued -- necessarily, because an ambiguous submit
outcome must never be retried automatically. On 2026-09-22 a canary was spent
on a submit that Bitfinex answered with HTTP 500 and that a venue query then
proved had placed nothing. That left halt 9 with: promotion needs a canary, the
canary needs a permit, the permit needs an epoch, reasserting the halt is a
no-op, and resume refuses a safety halt. Four correct guards composing into no
way forward at all.

Renewal is the audited operator action that grants another attempt. What it
must never become is a way to make a safety halt resumable.
"""
import pytest

from bfx_funding_bot.modules.execution.safety.halt_state import HaltStateStore
from tests.integration.test_capital_repository import capital_db as capital_db
from tests.integration.test_capital_repository import capital_engine as capital_engine

pytestmark = pytest.mark.asyncio


def _store(factory, account) -> HaltStateStore:
    return HaltStateStore(factory, account_id=str(account), deployment_environment="ci")


async def test_reasserting_a_halt_is_still_a_no_op(capital_db) -> None:
    """Guards re-assert constantly; that must keep costing nothing."""
    factory, account = capital_db
    store = _store(factory, account)
    first = await store.set_halted(True, reason="guard", actor="worker", kind="safety")
    again = await store.set_halted(True, reason="guard", actor="worker", kind="safety")
    assert again.created_at_ms == first.created_at_ms
    assert len(await store.history(limit=10)) == 1


async def test_renewal_advances_the_epoch_while_staying_halted(capital_db) -> None:
    factory, account = capital_db
    store = _store(factory, account)
    await store.set_halted(True, reason="release_blocked", actor="worker", kind="safety")
    renewed = await store.set_halted(
        True, reason="canary spent without placing an order", actor="operator", renew=True,
    )
    assert renewed.halted is True
    history = await store.history(limit=10)
    assert len(history) == 2
    assert history[0].reason == "canary spent without placing an order"


async def test_renewal_cannot_downgrade_a_safety_halt(capital_db) -> None:
    """The escalation this must not permit: renew as maintenance, then resume."""
    factory, account = capital_db
    store = _store(factory, account)
    await store.set_halted(True, reason="release_blocked", actor="worker", kind="safety")
    renewed = await store.set_halted(
        True, reason="another attempt", actor="operator", kind="maintenance", renew=True,
    )
    assert renewed.kind == "safety"


async def test_renewal_of_a_maintenance_halt_stays_maintenance(capital_db) -> None:
    """Carrying the kind forward is not the same as forcing it closed."""
    factory, account = capital_db
    store = _store(factory, account)
    await store.set_halted(True, reason="pg upgrade", actor="operator", kind="maintenance")
    renewed = await store.set_halted(
        True, reason="still upgrading", actor="operator", kind="safety", renew=True,
    )
    assert renewed.kind == "maintenance"


async def test_renewal_when_not_halted_behaves_as_an_ordinary_halt(capital_db) -> None:
    factory, account = capital_db
    store = _store(factory, account)
    state = await store.set_halted(
        True, reason="first", actor="operator", kind="safety", renew=True,
    )
    assert state.halted is True and state.kind == "safety"
    assert len(await store.history(limit=10)) == 1
