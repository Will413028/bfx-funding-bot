"""What the deploy tool says this process is (bfx-deploy's identity env).

``BFX_IMAGE_DIGEST``, ``BFX_SOURCE_REVISION`` and ``BFX_DEPLOYMENT_ID`` (the
``attempt_id`` of the ``deployments`` ledger row) identify the running build
for the audit trail and the status report. Nothing here gates trading: a
release never changes the trading state (lending envelope ADR 2026-09-25 D5).
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class DeploymentIdentity:
    """Validated identity; a missing or malformed part is None, never guessed."""

    backend_digest: str | None
    source_revision: str | None
    deployment_id: UUID | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> DeploymentIdentity:
        digest = (env.get("BFX_IMAGE_DIGEST") or "").strip()
        revision = (env.get("BFX_SOURCE_REVISION") or "").strip()
        try:
            deployment_id: UUID | None = UUID((env.get("BFX_DEPLOYMENT_ID") or "").strip())
        except ValueError:
            deployment_id = None
        return cls(digest if _DIGEST.fullmatch(digest) else None,
                   revision if _REVISION.fullmatch(revision) else None, deployment_id)


__all__ = ["DeploymentIdentity"]
