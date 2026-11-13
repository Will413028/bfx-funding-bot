"""DaemonTracing — OTLP trace spans on the daemon's key paths (observe-only).

Contract under test:
1. DEFAULT OFF: BFX_OTEL_ENABLED unset/false → `enabled` False and ZERO SDK
   initialization (the provider builder is never invoked).
2. FAIL-OPEN: broken tracer internals never raise into the traced path.
3. Enabled path (InMemorySpanExporter): spans exported with the agreed names,
   attrs (symbol/execution_decision_id/status …) and Resource attrs (service.name=bfx-bot,
   service.version, deployment.environment).
"""
from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from bfx_funding_bot.modules.observability.resource import (
    DeploymentEnvironment,
    EventResource,
)
from bfx_funding_bot.modules.observability.tracing import (
    DEFAULT_OTLP_ENDPOINT,
    DaemonTracing,
    tracing_from_env,
)


def _resource() -> EventResource:
    return EventResource(
        deployment_environment=DeploymentEnvironment.PROD,
        service_version="v-test-123",
        host_name=None,
    )


def _enabled_tracing(exporter: InMemorySpanExporter) -> DaemonTracing:
    return DaemonTracing(
        enabled=True,
        endpoint=DEFAULT_OTLP_ENDPOINT,
        event_resource=_resource(),
        exporter=exporter,
    )




# ── env parsing / default-off ────────────────────────────────────────────────


def test_from_env_default_is_disabled() -> None:
    t = tracing_from_env({}, event_resource=_resource())
    assert t.enabled is False
    assert t.endpoint == DEFAULT_OTLP_ENDPOINT == "http://tempo:4317"


@pytest.mark.parametrize("raw", ["false", "0", "no", "", "off", "junk"])
def test_from_env_disabled_values(raw: str) -> None:
    t = tracing_from_env({"BFX_OTEL_ENABLED": raw}, event_resource=_resource())
    assert t.enabled is False


@pytest.mark.parametrize("raw", ["true", "1", "TRUE", "yes", "on"])
def test_from_env_enabled_values(raw: str) -> None:
    t = tracing_from_env(
        {"BFX_OTEL_ENABLED": raw},
        event_resource=_resource(),
        exporter=InMemorySpanExporter(),
    )
    assert t.enabled is True
    t.shutdown()


def test_from_env_endpoint_override() -> None:
    t = tracing_from_env(
        {"BFX_OTEL_EXPORTER_ENDPOINT": "http://otherhost:4317"},
        event_resource=_resource(),
    )
    assert t.endpoint == "http://otherhost:4317"


def test_disabled_never_initializes_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """BFX_OTEL_ENABLED=false ⇒ zero SDK setup — the builder must not run."""

    def _boom(self: DaemonTracing) -> None:
        raise AssertionError("SDK initialization ran on the disabled path")

    monkeypatch.setattr(DaemonTracing, "_build_provider", _boom)
    t = tracing_from_env({"BFX_OTEL_ENABLED": "false"}, event_resource=_resource())
    assert t.enabled is False
    # span() on the disabled path is a pure no-op passthrough.
    with t.span("executor.submit", attributes={"bfx.symbol": "fUST"}) as span:
        span.set_attribute("k", "v")  # no-op, must not raise
    t.shutdown()  # no-op, must not raise


def test_disabled_span_is_transparent_to_exceptions() -> None:
    t = DaemonTracing(enabled=False, endpoint=DEFAULT_OTLP_ENDPOINT, event_resource=None)
    with pytest.raises(ValueError, match="boom"), t.span("x"):
        raise ValueError("boom")


# ── enabled path: spans + resource attrs (InMemorySpanExporter) ──────────────


def test_enabled_span_exported_with_resource_attrs() -> None:
    exporter = InMemorySpanExporter()
    t = _enabled_tracing(exporter)
    assert t.enabled is True
    with t.span("reconcile.tick") as span:
        span.set_attribute("bfx.reconcile.result", "ok")
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["reconcile.tick"]
    assert spans[0].attributes is not None
    assert spans[0].attributes["bfx.reconcile.result"] == "ok"
    res = spans[0].resource.attributes
    assert res["service.name"] == "bfx-bot"
    assert res["service.version"] == "v-test-123"
    assert res["deployment.environment"] == "prod"
    t.shutdown()


def test_enabled_span_records_exception_and_reraises() -> None:
    exporter = InMemorySpanExporter()
    t = _enabled_tracing(exporter)
    with pytest.raises(RuntimeError, match="kaput"), t.span("executor.submit"):
        raise RuntimeError("kaput")
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert [e.name for e in span.events] == ["exception"]
    t.shutdown()


def test_span_fail_open_when_tracer_broken() -> None:
    """A tracer that explodes at span start must not break the traced body."""
    exporter = InMemorySpanExporter()
    t = _enabled_tracing(exporter)

    class _BoomTracer:
        def start_as_current_span(self, *a: Any, **kw: Any) -> Any:
            raise RuntimeError("broken tracer")

    t._tracer = _BoomTracer()  # type: ignore[assignment]
    ran = False
    with t.span("executor.submit") as span:
        span.set_attribute("k", "v")  # safe no-op handle
        ran = True
    assert ran is True
    t.shutdown()


# ── shutdown ─────────────────────────────────────────────────────────────────


def test_shutdown_flushes_and_is_idempotent() -> None:
    exporter = InMemorySpanExporter()
    t = _enabled_tracing(exporter)
    with t.span("executor.submit"):
        pass
    t.shutdown()
    t.shutdown()  # second call must not raise
    assert len(exporter.get_finished_spans()) == 1


