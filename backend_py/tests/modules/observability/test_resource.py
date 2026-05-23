"""Tests for EventResource — OTEL-aligned process-level event metadata."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from bfx_funding_bot.modules.observability.resource import (
    ENVELOPE_SCHEMA_VERSION,
    DeploymentEnvironment,
    EventResource,
    _resolve_host_name,
    _resolve_service_version,
)


class TestDeploymentEnvironment:
    def test_enum_values_exhaustive(self) -> None:
        assert {e.value for e in DeploymentEnvironment} == {"prod", "shadow", "ci"}

    def test_enum_is_str_subclass(self) -> None:
        assert DeploymentEnvironment.PROD == "prod"
        assert str(DeploymentEnvironment.SHADOW) == "shadow"

    def test_invalid_env_raises(self) -> None:
        with pytest.raises(ValueError):
            DeploymentEnvironment("staging")


class TestResolveServiceVersion:
    def test_prefers_build_env(self) -> None:
        with patch.dict(os.environ, {"BFX_SERVICE_VERSION": "abc123"}):
            assert _resolve_service_version() == "abc123"

    def test_falls_back_to_git(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("subprocess.check_output", return_value="deadbee\n"),
        ):
            assert _resolve_service_version() == "deadbee"

    def test_unknown_when_no_git(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("subprocess.check_output", side_effect=FileNotFoundError),
        ):
            assert _resolve_service_version() == "unknown"


class TestResolveHostName:
    def test_returns_hostname_env(self) -> None:
        with patch.dict(os.environ, {"HOSTNAME": "marketfeed-abc"}):
            assert _resolve_host_name() == "marketfeed-abc"

    def test_returns_none_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert _resolve_host_name() is None


class TestEventResource:
    def test_immutability(self) -> None:
        r = EventResource(deployment_environment=DeploymentEnvironment.CI)
        with pytest.raises((AttributeError, Exception)):
            r.service_name = "hijacked"  # type: ignore[misc]

    def test_envelope_fields_otel_aligned_keys(self) -> None:
        r = EventResource(
            deployment_environment=DeploymentEnvironment.CI,
            service_name="bfx-funding-bot",
            service_version="abc123",
            host_name="host-1",
        )
        fields = r.envelope_fields()
        assert fields["schema_version"] == 1
        assert fields["deployment_environment"] == "ci"
        assert fields["service_name"] == "bfx-funding-bot"
        assert fields["service_version"] == "abc123"
        assert fields["host_name"] == "host-1"

    def test_envelope_fields_omits_host_when_none(self) -> None:
        r = EventResource(
            deployment_environment=DeploymentEnvironment.CI,
            service_version="abc",
            host_name=None,
        )
        fields = r.envelope_fields()
        assert "host_name" not in fields

    def test_schema_version_default_is_one(self) -> None:
        r = EventResource(deployment_environment=DeploymentEnvironment.PROD)
        assert r.schema_version == ENVELOPE_SCHEMA_VERSION == 1

    def test_to_otel_resource_keys_use_dots(self) -> None:
        """Lazy import: stub the otel module so test runs without OTEL SDK."""
        import sys
        from types import ModuleType

        stub_pkg = ModuleType("opentelemetry")
        stub_sdk = ModuleType("opentelemetry.sdk")
        stub_res = ModuleType("opentelemetry.sdk.resources")

        class FakeResource:
            def __init__(self, attrs: dict) -> None:
                self.attrs = attrs

            @classmethod
            def create(cls, attrs: dict) -> FakeResource:
                return cls(attrs)

        stub_res.Resource = FakeResource  # type: ignore[attr-defined]

        sys.modules["opentelemetry"] = stub_pkg
        sys.modules["opentelemetry.sdk"] = stub_sdk
        sys.modules["opentelemetry.sdk.resources"] = stub_res
        try:
            r = EventResource(
                deployment_environment=DeploymentEnvironment.PROD,
                service_version="v1",
                host_name="h1",
            )
            otel = r.to_otel_resource()
            assert otel.attrs == {
                "deployment.environment.name": "prod",
                "service.name": "bfx-funding-bot",
                "service.version": "v1",
                "host.name": "h1",
            }
        finally:
            del sys.modules["opentelemetry"]
            del sys.modules["opentelemetry.sdk"]
            del sys.modules["opentelemetry.sdk.resources"]
