from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pytest_httpx import HTTPXMock

from bfx_funding_bot.external import axiom as axiom_module
from bfx_funding_bot.external.axiom import (
    AxiomAuthError,
    AxiomClient,
    AxiomConfig,
)
from bfx_funding_bot.modules.observability.resource import (
    DeploymentEnvironment,
    EventResource,
)

_TEST_RESOURCE = EventResource(
    deployment_environment=DeploymentEnvironment.CI,
    service_version="test",
    host_name=None,
)


def _event() -> dict:
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "level": "info",
        "phase": "paper",
        "strategy": "mean_reversion",
        "cell": "fUSD_a30",
        "event_type": "signal",
        "correlation_id": str(uuid4()),
        "payload": {"signal_score": 0.5, "signal_direction": "post",
                    "strategy_attributes": {"rate": 0.0001}},
    }


@pytest.fixture
def cfg() -> AxiomConfig:
    return AxiomConfig(
        api_key="test-key",
        dataset="bfx-funding-bot-events",
        deployment_env=DeploymentEnvironment.CI,
        base_url="https://api.axiom.example",
        batch_size=2,
        flush_interval_s=0.05,
    )


async def test_emit_batches_to_ingest_endpoint(httpx_mock: HTTPXMock, cfg: AxiomConfig):
    httpx_mock.add_response(
        url="https://api.axiom.example/v1/datasets/bfx-funding-bot-events/ingest",
        method="POST", status_code=200, json={"ingested": 2},
    )
    client = AxiomClient(cfg, resource=_TEST_RESOURCE)
    await client.start()
    await client.emit(_event())
    await client.emit(_event())
    await client.flush()
    await client.stop()

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    body = json.loads(requests[0].read())
    assert len(body) == 2


async def test_emit_retries_on_500(httpx_mock: HTTPXMock, cfg: AxiomConfig):
    httpx_mock.add_response(status_code=500)
    httpx_mock.add_response(status_code=500)
    httpx_mock.add_response(status_code=200, json={"ingested": 1})

    client = AxiomClient(cfg, resource=_TEST_RESOURCE)
    await client.start()
    await client.emit(_event())
    await client.flush()
    await client.stop()

    assert len(httpx_mock.get_requests()) == 3


async def test_emit_401_raises_auth_error(httpx_mock: HTTPXMock, cfg: AxiomConfig):
    httpx_mock.add_response(status_code=401, json={"error": "unauthorized"})

    client = AxiomClient(cfg, resource=_TEST_RESOURCE)
    await client.start()
    await client.emit(_event())
    with pytest.raises(AxiomAuthError):
        await client.flush()
    await client.stop()


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
async def test_emit_falls_back_to_stdout_after_persistent_failure(
    httpx_mock: HTTPXMock, cfg: AxiomConfig, capsys: pytest.CaptureFixture
):
    for _ in range(10):
        httpx_mock.add_response(status_code=503)

    client = AxiomClient(cfg, resource=_TEST_RESOURCE)
    client._fallback_after_consecutive_fail = 2  # speed up test
    await client.start()
    await client.emit(_event())
    # Each flush() makes up to 3 HTTP attempts (tenacity retries).
    # With _fallback_after_consecutive_fail=2, fallback mode entered after
    # 2 batch-level failures (consuming 6 mock 503 responses).
    await client.flush()  # batch 1: 3 retries fail → _consecutive_fail=1
    await client.emit(_event())
    await client.flush()  # batch 2: 3 retries fail → _consecutive_fail=2 → fallback mode
    await client.stop()

    captured = capsys.readouterr()
    assert "axiom_fallback" in captured.out


async def test_on_flush_fires_per_loop_iteration_with_events(
    httpx_mock: HTTPXMock, cfg: AxiomConfig,
):
    """LIVENESS heartbeat (Phase 4.2.1+): on_flush is fired by _flush_loop
    after every iteration that completes flush(). Daemon wires this to
    HealthProbe.record_heartbeat("axiom") so scan_staleness sees a fresh
    last_active_ts when the loop task is alive.
    """
    httpx_mock.add_response(
        url="https://api.axiom.example/v1/datasets/bfx-funding-bot-events/ingest",
        method="POST", status_code=200, json={"ingested": 1},
    )
    calls: list[None] = []
    cfg2 = AxiomConfig(
        api_key=cfg.api_key, dataset=cfg.dataset,
        deployment_env=DeploymentEnvironment.CI,
        base_url=cfg.base_url,
        batch_size=cfg.batch_size, flush_interval_s=cfg.flush_interval_s,
        on_flush=lambda: calls.append(None),
    )
    client = AxiomClient(cfg2, resource=_TEST_RESOURCE)
    await client.start()
    await client.emit(_event())
    # Wait long enough for the loop to wake at least twice (cfg.flush_interval_s
    # = 0.05s → ~4 iterations in 0.2s, well under typical CI flake budget).
    await asyncio.sleep(0.2)
    await client.stop()

    assert len(calls) >= 2, f"expected loop to iterate >= 2x, got {len(calls)}"
    assert len(httpx_mock.get_requests()) == 1


async def test_on_flush_fires_per_loop_iteration_even_with_empty_queue(
    cfg: AxiomConfig,
):
    """LIVENESS heartbeat (Phase 4.2.1+): the loop must heartbeat in quiet
    periods (no events emitted) — otherwise the ~58min/hr inter-tick
    gaps between bursty axiom emits look like "task hung" to scan_staleness
    and trigger false-positive degraded warns. Inverts the previous
    PROGRESS semantic from 0f3dbe3 where on_flush only fired on non-empty
    flush.
    """
    calls: list[None] = []
    cfg2 = AxiomConfig(
        api_key=cfg.api_key, dataset=cfg.dataset,
        deployment_env=DeploymentEnvironment.CI,
        base_url=cfg.base_url,
        batch_size=cfg.batch_size, flush_interval_s=cfg.flush_interval_s,
        on_flush=lambda: calls.append(None),
    )
    client = AxiomClient(cfg2, resource=_TEST_RESOURCE)
    await client.start()
    # No emit. Loop iterates with empty queue.
    await asyncio.sleep(0.2)
    await client.stop()

    assert len(calls) >= 2, (
        f"expected loop liveness heartbeat to fire >= 2x with empty queue, "
        f"got {len(calls)}"
    )


async def test_on_flush_fires_in_fallback_mode(
    cfg: AxiomConfig, capsys: pytest.CaptureFixture,
):
    """Once fallback_mode=True, _send_batch returns after stdout emit (no
    HTTP, no retries). LIVENESS heartbeat still fires per loop iteration —
    fallback is a degraded but alive mode. (We short-circuit to fallback
    rather than triggering retries through 503s — tenacity exp-backoff
    timing dominates the test window otherwise.)"""
    calls: list[None] = []
    cfg2 = AxiomConfig(
        api_key=cfg.api_key, dataset=cfg.dataset,
        deployment_env=DeploymentEnvironment.CI,
        base_url=cfg.base_url,
        batch_size=cfg.batch_size, flush_interval_s=cfg.flush_interval_s,
        on_flush=lambda: calls.append(None),
    )
    client = AxiomClient(cfg2, resource=_TEST_RESOURCE)
    client._fallback_mode = True  # short-circuit; _send_batch goes stdout-only
    await client.start()
    await client.emit(_event())
    await asyncio.sleep(0.2)
    await client.stop()

    assert len(calls) >= 2
    captured = capsys.readouterr()
    assert "axiom_fallback" in captured.out


async def test_on_flush_stops_after_auth_error_kills_flush_loop(
    httpx_mock: HTTPXMock, cfg: AxiomConfig, monkeypatch: pytest.MonkeyPatch,
):
    """AxiomAuthError raised by flush() inside _flush_loop kills the loop
    task (after raising SIGTERM for daemon graceful shutdown). LIVENESS
    heartbeat consequence: no more on_flush fires → scan_staleness sees
    axiom heartbeat decay → escalates correctly.
    """
    httpx_mock.add_response(status_code=401, json={"error": "unauthorized"})
    # Prevent SIGTERM from killing pytest — only assert behavioral outcome.
    monkeypatch.setattr(
        axiom_module._signal, "raise_signal", lambda _sig: None,
    )
    calls: list[None] = []
    cfg2 = AxiomConfig(
        api_key=cfg.api_key, dataset=cfg.dataset,
        deployment_env=DeploymentEnvironment.CI,
        base_url=cfg.base_url,
        batch_size=cfg.batch_size, flush_interval_s=cfg.flush_interval_s,
        on_flush=lambda: calls.append(None),
    )
    client = AxiomClient(cfg2, resource=_TEST_RESOURCE)
    await client.start()
    await client.emit(_event())
    # Loop wakes, calls flush() which raises AxiomAuthError → _flush_loop
    # outer except handles SIGTERM (mocked) and re-raises → task done.
    await asyncio.sleep(0.2)
    assert client._flush_task is not None
    assert client._flush_task.done()
    fires_before = len(calls)
    # Confirm: no further heartbeats after task death.
    await asyncio.sleep(0.1)
    assert len(calls) == fires_before, (
        f"on_flush should not fire after _flush_loop dies; "
        f"got {fires_before} → {len(calls)}"
    )
    with contextlib.suppress(AxiomAuthError):
        await client.stop()
