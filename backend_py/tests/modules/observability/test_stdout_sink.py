"""StdoutEventSink: operational telemetry → structured stdout (3c)."""
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.observability.resource import DeploymentEnvironment, EventResource
from bfx_funding_bot.modules.observability.stdout_sink import StdoutEventSink


def _resource() -> EventResource:
    # mirror daemon.py 的建構參數；deployment_environment 是 envelope_fields 的一員
    # Must use DeploymentEnvironment enum (frozen dataclass does not coerce str)
    return EventResource(deployment_environment=DeploymentEnvironment.CI)


@pytest.mark.asyncio
async def test_emit_writes_single_json_line_with_envelope_fields(caplog) -> None:
    sink = StdoutEventSink(resource=_resource())
    with caplog.at_level(logging.INFO, logger="bfx_funding_bot.events"):
        await sink.emit({"event_type": "signal", "payload": {"score": 0.42}})

    assert len(caplog.records) == 1
    line = json.loads(caplog.records[0].getMessage())
    assert line["event_type"] == "signal"
    assert line["payload"] == {"score": 0.42}
    # envelope enrichment：deployment_environment 來自 resource
    assert line["deployment_environment"] == "ci"


@pytest.mark.asyncio
async def test_resource_fields_win_over_event_on_collision(caplog) -> None:
    # AxiomClient merge direction: {**event, **resource_fields} → resource wins.
    # StdoutEventSink must mirror this: resource fields overwrite event-supplied keys.
    sink = StdoutEventSink(resource=_resource())
    with caplog.at_level(logging.INFO, logger="bfx_funding_bot.events"):
        await sink.emit({"event_type": "x", "deployment_environment": "shadow"})
    line = json.loads(caplog.records[0].getMessage())
    # resource ("ci") overwrites the event-supplied "shadow" — mirror AxiomClient
    assert line["deployment_environment"] == "ci"


@pytest.mark.asyncio
async def test_emit_is_lossless_and_json_safe(caplog) -> None:
    # 含 Decimal/datetime 等非原生 JSON 型別不可炸（default=str）
    sink = StdoutEventSink(resource=_resource())
    with caplog.at_level(logging.INFO, logger="bfx_funding_bot.events"):
        await sink.emit({"amt": Decimal("1.5"), "ts": datetime(2026, 5, 24, tzinfo=UTC)})
    line = json.loads(caplog.records[0].getMessage())
    assert line["amt"] == "1.5"


# ── Four Golden Signals metrics hook (observe-only, fail-open) ────────────────


@pytest.mark.asyncio
async def test_emit_counts_operational_event_when_metrics_bound(caplog) -> None:
    from bfx_funding_bot.modules.observability.metrics import DaemonMetrics

    metrics = DaemonMetrics()
    sink = StdoutEventSink(resource=_resource(), metrics=metrics)
    with caplog.at_level(logging.INFO, logger="bfx_funding_bot.events"):
        await sink.emit({"event_type": "signal", "level": "info"})

    assert metrics.registry.get_sample_value(
        "bfx_operational_events_total", {"event_type": "signal", "level": "info"},
    ) == 1.0
    assert len(caplog.records) == 1  # stdout line still written


@pytest.mark.asyncio
async def test_emit_still_logs_when_metrics_hook_raises(caplog) -> None:
    """Fail-open: a broken metrics hook must never eat the telemetry line."""

    class _BrokenMetrics:
        def observe_operational_event(self, event) -> None:
            raise RuntimeError("metrics down")

    sink = StdoutEventSink(resource=_resource(), metrics=_BrokenMetrics())
    with caplog.at_level(logging.INFO, logger="bfx_funding_bot.events"):
        await sink.emit({"event_type": "signal", "level": "info"})
    assert len(caplog.records) == 1
