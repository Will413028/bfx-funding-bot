"""StdoutEventSink — operational telemetry sink (3c, replaces AxiomClient).

Drop-in for the `emit(dict)` port shared with the (removed) AxiomClient and the
DiagnosticsSink. Writes one JSON line per event to a dedicated stdout logger so
the deploy platform (Koyeb) can route/retain it. No background flush loop:
emit() is synchronous, so the daemon no longer needs an "axiom" sub-task.

Merge direction mirrors AxiomClient: {**event, **resource.envelope_fields()},
meaning resource envelope fields win over any event-supplied keys with the same
name. This is the drop-in behavioral parity contract.

Operational events only (SIGNAL / HEALTH_CHECK / ORDER_SUBMIT / lifecycle).
Forensic events go to DiagnosticsSink (PG); SoT events go to event_log.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from bfx_funding_bot.modules.observability.resource import EventResource

# Named (not __name__) so Koyeb can route this stream separately from app logs.
log = logging.getLogger("bfx_funding_bot.events")
# Module logger for the sink's own failure paths (kept off the events stream).
_module_log = logging.getLogger(__name__)


class _MetricsHook(Protocol):
    """Structural port for DaemonMetrics — avoids a hard observability→metrics
    import at type level and keeps test doubles trivial."""

    def observe_operational_event(self, event: dict[str, Any]) -> None: ...


class StdoutEventSink:
    """Structured-stdout operational event sink.

    Optional `metrics` hook (Four Golden Signals): every emitted event bumps
    bfx_operational_events_total{event_type,level}. Observe-only + fail-open —
    a broken hook never suppresses the stdout telemetry line.
    """

    def __init__(
        self, *, resource: EventResource, metrics: _MetricsHook | None = None,
    ) -> None:
        self._resource = resource
        self._metrics = metrics

    async def emit(self, event: dict[str, Any]) -> None:
        if self._metrics is not None:
            try:
                self._metrics.observe_operational_event(event)
            except Exception:
                _module_log.debug("stdout_sink_metrics_hook_failed", exc_info=True)
        enriched = {**event, **self._resource.envelope_fields()}
        log.info(json.dumps(enriched, default=str))
