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
from typing import Any

from bfx_funding_bot.modules.observability.resource import EventResource

log = logging.getLogger("bfx_funding_bot.events")


class StdoutEventSink:
    """Structured-stdout operational event sink."""

    def __init__(self, *, resource: EventResource) -> None:
        self._resource = resource

    async def emit(self, event: dict[str, Any]) -> None:
        # resource envelope fields win over event-supplied keys, mirroring
        # AxiomClient's merge: {**event, **self._resource.envelope_fields()}
        enriched = {**event, **self._resource.envelope_fields()}
        log.info(json.dumps(enriched, default=str))
