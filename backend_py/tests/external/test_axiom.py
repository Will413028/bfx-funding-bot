from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pytest_httpx import HTTPXMock

from bfx_funding_bot.external.axiom import (
    AxiomAuthError,
    AxiomClient,
    AxiomConfig,
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
        base_url="https://api.axiom.example",
        batch_size=2,
        flush_interval_s=0.05,
    )


async def test_emit_batches_to_ingest_endpoint(httpx_mock: HTTPXMock, cfg: AxiomConfig):
    httpx_mock.add_response(
        url="https://api.axiom.example/v1/datasets/bfx-funding-bot-events/ingest",
        method="POST", status_code=200, json={"ingested": 2},
    )
    client = AxiomClient(cfg)
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

    client = AxiomClient(cfg)
    await client.start()
    await client.emit(_event())
    await client.flush()
    await client.stop()

    assert len(httpx_mock.get_requests()) == 3


async def test_emit_401_raises_auth_error(httpx_mock: HTTPXMock, cfg: AxiomConfig):
    httpx_mock.add_response(status_code=401, json={"error": "unauthorized"})

    client = AxiomClient(cfg)
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

    client = AxiomClient(cfg)
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
