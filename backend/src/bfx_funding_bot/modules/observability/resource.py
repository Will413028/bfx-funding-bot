"""EventResource: OTEL-aligned process-level event metadata.

Aligned with OpenTelemetry Resource semantic conventions:
- deployment.environment.name → deployment_environment
- service.name → service_name
- service.version → service_version
- host.name → host_name (Optional)

Future OTEL SDK adoption (Pending #4) uses to_otel_resource() — zero schema churn.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

ENVELOPE_SCHEMA_VERSION: int = 1
"""Bump on breaking envelope shape changes. See spec D18."""


class DeploymentEnvironment(StrEnum):
    PROD = "prod"
    SHADOW = "shadow"
    CI = "ci"


def _resolve_service_version() -> str:
    """Build-time env first; git fallback for local dev; "unknown" last resort."""
    v = os.environ.get("BFX_SERVICE_VERSION")
    if v:
        return v
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


def _resolve_host_name() -> str | None:
    """Koyeb sets HOSTNAME to instance ID; None locally is fine."""
    return os.environ.get("HOSTNAME")


@dataclass(frozen=True)
class EventResource:
    deployment_environment: DeploymentEnvironment
    service_name: str = "bfx-funding-bot"
    service_version: str = field(default_factory=_resolve_service_version)
    host_name: str | None = field(default_factory=_resolve_host_name)
    schema_version: int = ENVELOPE_SCHEMA_VERSION

    def envelope_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "schema_version": self.schema_version,
            "deployment_environment": self.deployment_environment.value,
            "service_name": self.service_name,
            "service_version": self.service_version,
        }
        if self.host_name is not None:
            fields["host_name"] = self.host_name
        return fields

    def to_otel_resource(self) -> Any:
        """Migration helper for future OTEL adoption.

        Lazy import keeps SDK cost off the disabled path. Since OTEL adoption
        (wiki pending #4) the SDK is a real dependency — see tracing.py.
        """
        from opentelemetry.sdk.resources import Resource

        attrs: dict[str, Any] = {
            "deployment.environment.name": self.deployment_environment.value,
            "service.name": self.service_name,
            "service.version": self.service_version,
        }
        if self.host_name is not None:
            attrs["host.name"] = self.host_name
        return Resource.create(attrs)
