"""Tests for AxiomClient resource injection at emit() boundary."""
from __future__ import annotations

import pytest

from bfx_funding_bot.external.axiom import AxiomClient, AxiomConfig
from bfx_funding_bot.modules.observability.resource import (
    DeploymentEnvironment,
    EventResource,
)


@pytest.fixture
def cfg() -> AxiomConfig:
    return AxiomConfig(
        api_key="axk_test",
        dataset="bfx-funding-bot-ci",
        deployment_env=DeploymentEnvironment.CI,
    )


@pytest.fixture
def resource() -> EventResource:
    return EventResource(
        deployment_environment=DeploymentEnvironment.CI,
        service_version="abc123",
        host_name=None,
    )


class TestEmitEnrichesEnvelope:
    @pytest.mark.asyncio
    async def test_resource_fields_injected_at_top_level(
        self, cfg: AxiomConfig, resource: EventResource
    ) -> None:
        client = AxiomClient(cfg=cfg, resource=resource)
        await client.emit({"type": "order_fill", "_time": 1, "payload": {"cid": 1}})
        event = client._queue.get_nowait()
        assert event["type"] == "order_fill"
        assert event["_time"] == 1
        assert event["payload"] == {"cid": 1}
        assert event["deployment_environment"] == "ci"
        assert event["service_name"] == "bfx-funding-bot"
        assert event["service_version"] == "abc123"
        assert event["schema_version"] == 1

    @pytest.mark.asyncio
    async def test_payload_untouched(
        self, cfg: AxiomConfig, resource: EventResource
    ) -> None:
        client = AxiomClient(cfg=cfg, resource=resource)
        payload = {"cid": 42, "nested": {"foo": "bar"}}
        await client.emit({"type": "x", "_time": 1, "payload": payload})
        event = client._queue.get_nowait()
        assert event["payload"] == payload

    @pytest.mark.asyncio
    async def test_reserved_field_collision_raises(
        self, cfg: AxiomConfig, resource: EventResource
    ) -> None:
        client = AxiomClient(cfg=cfg, resource=resource)
        with pytest.raises(ValueError, match="reserved resource field"):
            await client.emit(
                {
                    "type": "x",
                    "_time": 1,
                    "deployment_environment": "prod",
                    "payload": {},
                }
            )

    @pytest.mark.asyncio
    async def test_host_name_omitted_when_resource_has_none(
        self, cfg: AxiomConfig, resource: EventResource
    ) -> None:
        client = AxiomClient(cfg=cfg, resource=resource)
        await client.emit({"type": "x", "_time": 1, "payload": {}})
        event = client._queue.get_nowait()
        assert "host_name" not in event

    @pytest.mark.asyncio
    async def test_host_name_emitted_when_resource_has_value(
        self, cfg: AxiomConfig
    ) -> None:
        resource = EventResource(
            deployment_environment=DeploymentEnvironment.CI,
            service_version="abc",
            host_name="koyeb-instance-xyz",
        )
        client = AxiomClient(cfg=cfg, resource=resource)
        await client.emit({"type": "x", "_time": 1, "payload": {}})
        event = client._queue.get_nowait()
        assert event["host_name"] == "koyeb-instance-xyz"
