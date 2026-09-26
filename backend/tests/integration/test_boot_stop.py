"""A refused live boot alerts and writes nothing (lending envelope D3/D5).

A durable HALTED would outlive the cause and make the next, fixed build wait
for an operator; nothing reaches the venue because a refused process has no
command gate to prove which offers are its own.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.safety import boot_stop
from bfx_funding_bot.modules.execution.safety.boot_stop import (
    VENUE_OFFERS_MAY_REMAIN,
    report_refused_boot,
)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    captured: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(boot_stop.alerts, "emit",
                        lambda event, **fields: captured.append((event, fields)))
    return captured


def test_a_refused_boot_tells_the_operator_offers_may_remain(sent) -> None:
    account = uuid4()
    report_refused_boot(account_id=account, environment="ci",
                        reason="boot_blocked: schema head mismatch")
    assert [event for event, _ in sent] == [VENUE_OFFERS_MAY_REMAIN]
    fields = sent[0][1]
    assert fields["level"] == "critical" and fields["account"] == str(account)
    assert "kill switch" in fields["message"]
