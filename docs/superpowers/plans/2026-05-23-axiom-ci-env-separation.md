# Axiom CI Integration + Observability Env Separation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build CI integration test infrastructure for `AxiomReplayQueryAdapter` (catch wire-level bugs in PR phase), plus split single prod Axiom dataset into 3 env-isolated datasets (prod / shadow / ci) with OTEL-aligned `EventResource` envelope.

**Architecture:** Process-level `EventResource` dataclass (OTEL Resource pattern) injected into `AxiomClient` at init; emit-side merges resource fields into every event envelope before queue push; query-side `AxiomReplayQueryAdapter` filters by `deployment_environment`. CI runs both unit (mocked) + integration (real Axiom CI dataset) jobs on PR + main push. Shadow Koyeb service migrated to dedicated dataset before Phase 4.4 canary.

**Tech Stack:** Python 3.13, uv, pytest, httpx, pydantic, tenacity, dataclasses, StrEnum, GitHub Actions, Axiom.

**Spec:** `docs/superpowers/specs/2026-05-23-axiom-ci-env-separation-design.md`

---

## File Structure

**Create:**
- `backend_py/src/bfx_funding_bot/modules/observability/__init__.py` — module init
- `backend_py/src/bfx_funding_bot/modules/observability/resource.py` — `EventResource` + `DeploymentEnvironment` + helpers
- `backend_py/tests/modules/observability/__init__.py` — test pkg
- `backend_py/tests/modules/observability/test_resource.py` — `EventResource` unit tests
- `backend_py/tests/modules/observability/test_axiom_client_resource.py` — `AxiomClient` resource injection tests
- `backend_py/tests/modules/marketfeed/test_axiom_config.py` — `AxiomConfig.from_env` tests
- `backend_py/tests/modules/marketfeed/test_daemon_axiom_wiring.py` — `build_daemon` two-end symmetry test
- `.github/dependabot.yml` — Actions SHA pin auto-bump
- `backend_py/docs/runbooks/observability-env-setup.md` — local dev + cutover runbook

**Modify:**
- `backend_py/src/bfx_funding_bot/external/axiom.py` — `AxiomConfig` add `deployment_env` + `from_env` classmethod; `AxiomClient.__init__` accept `resource`; `AxiomClient.emit` merge resource fields
- `backend_py/src/bfx_funding_bot/modules/execution/axiom_event_query.py` — `AxiomReplayQueryAdapter.__init__` accept `deployment_environment`; `_build_apl` add `where deployment_environment` clause
- `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py` — `build_daemon` build `EventResource`, wire into both `AxiomClient` + `AxiomReplayQueryAdapter`
- `backend_py/tests/modules/execution/test_axiom_event_query.py` — extend with env filter assertions
- `backend_py/tests/integration/test_axiom_replay_query_real.py` — T12 upgrade (run-scoped account_id + polling + env/schema_version asserts)
- `backend_py/tests/integration/test_daemon_shutdown.py` — `xfail(strict=False)` markers
- `backend_py/tests/integration/test_path_e_subprocess.py` — `xfail(strict=False)` marker
- `backend_py/tests/integration/test_warmup_determinism.py` — `xfail(strict=False)` marker
- `backend_py/Dockerfile` — `ARG GIT_SHA` + `ENV BFX_SERVICE_VERSION`
- `.github/workflows/ci.yml` — add `backend_py_unit` + `backend_py_integration` jobs

---

## Execution Order Dependencies

Tasks must run in this order due to type/wiring dependencies:
1. **Task 1 (EventResource)** — defines types used by 2, 3, 4, 5, 7
2. **Task 2 (AxiomConfig)** — uses `DeploymentEnvironment` from Task 1
3. **Task 3 (AxiomClient)** — uses `EventResource` from Task 1, `AxiomConfig` from Task 2
4. **Task 4 (AxiomReplayQueryAdapter)** — uses `DeploymentEnvironment` from Task 1
5. **Task 5 (daemon wiring)** — uses all of 1, 2, 3, 4
6. **Task 6 (Dockerfile)** — independent, can run anytime
7. **Task 7 (T12 upgrade)** — uses 1, 3, 4, 5
8. **Task 8 (xfail markers)** — independent
9. **Task 9 (CI workflow)** — uses test markers from 8, but logically last (needs all tests to pass first)
10. **Task 10 (dependabot.yml)** — independent
11. **Task 11 (runbook)** — independent, finalizes after 9 done

After all code tasks pass, follow **Manual Cutover Steps** appendix (Axiom dataset / GitHub secrets / Koyeb env update).

---

## Task 1: Create `EventResource` module

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/observability/__init__.py`
- Create: `backend_py/src/bfx_funding_bot/modules/observability/resource.py`
- Create: `backend_py/tests/modules/observability/__init__.py`
- Create: `backend_py/tests/modules/observability/test_resource.py`

- [ ] **Step 1: Create empty package init files**

```bash
mkdir -p backend_py/src/bfx_funding_bot/modules/observability
mkdir -p backend_py/tests/modules/observability
touch backend_py/src/bfx_funding_bot/modules/observability/__init__.py
touch backend_py/tests/modules/observability/__init__.py
```

- [ ] **Step 2: Write failing test file**

Create `backend_py/tests/modules/observability/test_resource.py`:

```python
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
        with patch.dict(os.environ, {}, clear=True):
            with patch("subprocess.check_output", return_value="deadbee\n"):
                assert _resolve_service_version() == "deadbee"

    def test_unknown_when_no_git(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch("subprocess.check_output", side_effect=FileNotFoundError):
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
            def create(cls, attrs: dict) -> "FakeResource":
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
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd backend_py && uv run pytest tests/modules/observability/test_resource.py -v
```

Expected: FAIL with `ImportError: cannot import name 'EventResource' from 'bfx_funding_bot.modules.observability.resource'`.

- [ ] **Step 4: Write minimal `resource.py` implementation**

Create `backend_py/src/bfx_funding_bot/modules/observability/resource.py`:

```python
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

        Lazy imports `opentelemetry.sdk.resources.Resource` — no SDK
        dependency until OTEL adoption phase. See spec D20.
        """
        from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]

        attrs: dict[str, Any] = {
            "deployment.environment.name": self.deployment_environment.value,
            "service.name": self.service_name,
            "service.version": self.service_version,
        }
        if self.host_name is not None:
            attrs["host.name"] = self.host_name
        return Resource.create(attrs)
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd backend_py && uv run pytest tests/modules/observability/test_resource.py -v
```

Expected: All 11 tests PASS.

- [ ] **Step 6: Type-check + lint**

```bash
cd backend_py && uv run mypy --strict src/bfx_funding_bot/modules/observability && uv run ruff check src/bfx_funding_bot/modules/observability tests/modules/observability
```

Expected: mypy clean + ruff clean.

- [ ] **Step 7: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/observability backend_py/tests/modules/observability
git commit -m "✨ Feat: EventResource OTEL-aligned envelope metadata (T12 spec D3/D18/D19/D20)"
```

---

## Task 2: AxiomConfig adds `deployment_env` + `from_env` classmethod

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/external/axiom.py:41-66` (AxiomConfig dataclass)
- Create: `backend_py/tests/modules/marketfeed/test_axiom_config.py`

Note: keep @dataclass form (spec Shape B) — minimal change, matches current pattern. `pydantic-settings>=2.6` already in deps but AxiomConfig is plain `@dataclass`.

- [ ] **Step 1: Write failing test file**

Create `backend_py/tests/modules/marketfeed/test_axiom_config.py`:

```python
"""Tests for AxiomConfig.from_env — env-binding with fail-loud validation."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from bfx_funding_bot.external.axiom import AxiomConfig
from bfx_funding_bot.modules.observability.resource import DeploymentEnvironment


class TestFromEnv:
    @pytest.fixture
    def base_env(self) -> dict[str, str]:
        return {
            "AXIOM_API_KEY": "axk_test",
            "AXIOM_DATASET": "bfx-funding-bot-ci",
            "BFX_DEPLOYMENT_ENV": "ci",
        }

    def test_happy_path(self, base_env: dict[str, str]) -> None:
        with patch.dict(os.environ, base_env, clear=True):
            cfg = AxiomConfig.from_env()
        assert cfg.api_key == "axk_test"
        assert cfg.dataset == "bfx-funding-bot-ci"
        assert cfg.deployment_env is DeploymentEnvironment.CI

    @pytest.mark.parametrize("env_value", ["prod", "shadow", "ci"])
    def test_all_three_envs_accepted(
        self, base_env: dict[str, str], env_value: str
    ) -> None:
        env = {**base_env, "BFX_DEPLOYMENT_ENV": env_value}
        with patch.dict(os.environ, env, clear=True):
            cfg = AxiomConfig.from_env()
        assert cfg.deployment_env.value == env_value

    def test_missing_deployment_env_raises(self, base_env: dict[str, str]) -> None:
        env = {k: v for k, v in base_env.items() if k != "BFX_DEPLOYMENT_ENV"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValueError, match="BFX_DEPLOYMENT_ENV"):
                AxiomConfig.from_env()

    def test_missing_api_key_raises(self, base_env: dict[str, str]) -> None:
        env = {k: v for k, v in base_env.items() if k != "AXIOM_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(KeyError, match="AXIOM_API_KEY"):
                AxiomConfig.from_env()

    def test_invalid_deployment_env_raises(self, base_env: dict[str, str]) -> None:
        env = {**base_env, "BFX_DEPLOYMENT_ENV": "staging"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValueError):
                AxiomConfig.from_env()
```

Also ensure `backend_py/tests/modules/marketfeed/__init__.py` exists (likely already does):

```bash
test -f backend_py/tests/modules/marketfeed/__init__.py || touch backend_py/tests/modules/marketfeed/__init__.py
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend_py && uv run pytest tests/modules/marketfeed/test_axiom_config.py -v
```

Expected: FAIL with `AttributeError: type object 'AxiomConfig' has no attribute 'from_env'`.

- [ ] **Step 3: Modify AxiomConfig to add `deployment_env` + `from_env`**

Edit `backend_py/src/bfx_funding_bot/external/axiom.py`. Find the existing `AxiomConfig` dataclass (around line 41-66) and replace with:

```python
@dataclass
class AxiomConfig:
    api_key: str
    dataset: str
    deployment_env: DeploymentEnvironment
    base_url: str = "https://api.axiom.co"
    batch_size: int = 100
    flush_interval_s: float = 1.0
    request_timeout_s: float = 10.0
    # LIVENESS heartbeat (Phase 4.2.1+ tuning): fired by AxiomClient after
    # every _flush_loop iteration regardless of batch size. Daemon wires
    # to record_heartbeat("axiom").
    on_flush: Callable[[], None] | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "AxiomConfig":
        env_val = os.environ.get("BFX_DEPLOYMENT_ENV")
        if not env_val:
            raise ValueError(
                "BFX_DEPLOYMENT_ENV required (one of: prod, shadow, ci)"
            )
        return cls(
            api_key=os.environ["AXIOM_API_KEY"],
            dataset=os.environ["AXIOM_DATASET"],
            deployment_env=DeploymentEnvironment(env_val),
        )
```

Add to file imports (top of `axiom.py`):

```python
import os

from bfx_funding_bot.modules.observability.resource import DeploymentEnvironment
```

- [ ] **Step 4: Update callers — existing `AxiomConfig(...)` instantiation sites**

Find existing instantiations:

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot && grep -rn "AxiomConfig(" backend_py/src backend_py/tests --include="*.py" | grep -v test_axiom_config
```

For each call site (likely in `daemon.py` and existing tests):
- If it's a test fixture or test mock — add `deployment_env=DeploymentEnvironment.CI` (or appropriate value)
- If it's production daemon wiring — defer to Task 5 (daemon.py change handles this)

Make minimum patch to existing callers so all existing tests still pass.

- [ ] **Step 5: Run config test**

```bash
cd backend_py && uv run pytest tests/modules/marketfeed/test_axiom_config.py -v
```

Expected: All 6 tests PASS.

- [ ] **Step 6: Run full unit test suite to catch caller breakage**

```bash
cd backend_py && uv run pytest -m "not integration" -q
```

Expected: All pass. Any fail = a caller instantiates `AxiomConfig` without `deployment_env` — patch it.

- [ ] **Step 7: Type-check**

```bash
cd backend_py && uv run mypy --strict src/bfx_funding_bot/external/axiom.py && uv run ruff check src/bfx_funding_bot/external/axiom.py tests/modules/marketfeed/test_axiom_config.py
```

Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add backend_py/src/bfx_funding_bot/external/axiom.py backend_py/tests/modules/marketfeed/test_axiom_config.py
# Also stage any caller patches you made in step 4
git commit -m "✨ Feat: AxiomConfig.deployment_env + from_env classmethod (T12 spec D5)"
```

---

## Task 3: AxiomClient envelope enrichment via EventResource

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/external/axiom.py` (AxiomClient class)
- Create: `backend_py/tests/modules/observability/test_axiom_client_resource.py`

**Design**: Resource fields injected at `emit()` boundary (caller→client) so:
- stdout fallback events also get resource fields (consistency)
- Single enrichment point
- Caller doesn't need to know about resource

Caller-supplied event MUST NOT contain reserved resource field names (`schema_version` / `deployment_environment` / `service_name` / `service_version` / `host_name`) — raise `ValueError` if collision (fail-loud internal contract).

- [ ] **Step 1: Write failing test file**

Create `backend_py/tests/modules/observability/test_axiom_client_resource.py`:

```python
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
        # Drain queue
        event = client._queue.get_nowait()
        assert event["type"] == "order_fill"  # preserved
        assert event["_time"] == 1
        assert event["payload"] == {"cid": 1}  # payload untouched
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
        # Payload reference preserved verbatim
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
                    "deployment_environment": "prod",  # collision
                    "payload": {},
                }
            )

    @pytest.mark.asyncio
    async def test_host_name_omitted_when_resource_has_none(
        self, cfg: AxiomConfig, resource: EventResource
    ) -> None:
        # resource fixture has host_name=None
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend_py && uv run pytest tests/modules/observability/test_axiom_client_resource.py -v
```

Expected: FAIL — `AxiomClient.__init__()` takes 2 positional args but resource was passed.

- [ ] **Step 3: Modify `AxiomClient.__init__` + `emit()`**

In `backend_py/src/bfx_funding_bot/external/axiom.py`, find the `AxiomClient` class. Add new imports at top (if not present):

```python
from bfx_funding_bot.modules.observability.resource import EventResource
```

Modify `__init__`:

```python
class AxiomClient:
    _RESERVED_ENVELOPE_FIELDS = frozenset({
        "schema_version",
        "deployment_environment",
        "service_name",
        "service_version",
        "host_name",
    })

    def __init__(self, cfg: AxiomConfig, resource: EventResource) -> None:
        self.cfg = cfg
        self._resource = resource
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._http: httpx.AsyncClient | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._fallback_mode = False
        self._consecutive_fail = 0
        self._fallback_after_consecutive_fail = 3
```

Modify `emit()`:

```python
async def emit(self, event: dict[str, Any]) -> None:
    collisions = self._RESERVED_ENVELOPE_FIELDS & event.keys()
    if collisions:
        raise ValueError(
            f"emit() got reserved resource field(s) in event: {sorted(collisions)}. "
            f"Resource fields are injected by AxiomClient; do not set them in callers."
        )
    enriched = {**event, **self._resource.envelope_fields()}
    await self._queue.put(enriched)
```

- [ ] **Step 4: Patch existing AxiomClient callers**

Find existing instantiations:

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot && grep -rn "AxiomClient(" backend_py/src backend_py/tests --include="*.py"
```

For each:
- Production code (likely `daemon.py`) → deferred to Task 5
- Test fixtures → add `resource=EventResource(deployment_environment=DeploymentEnvironment.CI, service_version="test")` (use stable test value to avoid git subprocess in fast unit tests)

- [ ] **Step 5: Run resource injection test**

```bash
cd backend_py && uv run pytest tests/modules/observability/test_axiom_client_resource.py -v
```

Expected: All 5 tests PASS.

- [ ] **Step 6: Run full unit test suite**

```bash
cd backend_py && uv run pytest -m "not integration" -q
```

Expected: PASS. Any fail = unpatched `AxiomClient(...)` caller — patch it.

- [ ] **Step 7: Type-check + lint**

```bash
cd backend_py && uv run mypy --strict src/bfx_funding_bot/external/axiom.py && uv run ruff check src/bfx_funding_bot/external/axiom.py tests/modules/observability/test_axiom_client_resource.py
```

Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add backend_py/src/bfx_funding_bot/external/axiom.py backend_py/tests/modules/observability/test_axiom_client_resource.py
# stage any test fixture patches from step 4
git commit -m "✨ Feat: AxiomClient injects EventResource into envelope at emit() (T12 spec D3)"
```

---

## Task 4: AxiomReplayQueryAdapter deployment_environment filter

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/axiom_event_query.py:24-38` (`__init__`) + `_build_apl` (lines ~67-115)
- Modify: `backend_py/tests/modules/execution/test_axiom_event_query.py` (extend existing)

- [ ] **Step 1: Inspect existing `_build_apl` to confirm exact location**

```bash
cd backend_py && grep -n "_build_apl\|def query_order_events\|def fetch_events\|where" src/bfx_funding_bot/modules/execution/axiom_event_query.py
```

Note line numbers of `_build_apl` (or whatever method builds APL string). It's likely 1-2 helper methods.

- [ ] **Step 2: Write failing test additions**

In `backend_py/tests/modules/execution/test_axiom_event_query.py`, append new test class:

```python
class TestDeploymentEnvironmentFilter:
    def test_init_accepts_deployment_environment(self) -> None:
        adapter = AxiomReplayQueryAdapter(
            api_key="axk_test",
            dataset="bfx-funding-bot-ci",
            deployment_environment=DeploymentEnvironment.CI,
        )
        assert adapter._deployment_environment is DeploymentEnvironment.CI

    @pytest.mark.asyncio
    async def test_query_order_events_apl_includes_env_filter(
        self, respx_mock
    ) -> None:
        # Capture APL string sent to Axiom
        captured: dict[str, str] = {}

        def _capture(request):
            import json

            body = json.loads(request.content)
            captured["apl"] = body.get("apl", "")
            return httpx.Response(200, json={"tables": []})

        respx_mock.post("/v1/datasets/_apl").mock(side_effect=_capture)

        adapter = AxiomReplayQueryAdapter(
            api_key="axk_test",
            dataset="bfx-funding-bot-ci",
            deployment_environment=DeploymentEnvironment.CI,
        )
        await adapter.query_order_events(
            account_id="ci-run-1", since=datetime(2026, 5, 23, tzinfo=UTC)
        )
        assert 'deployment_environment == "ci"' in captured["apl"]

    @pytest.mark.asyncio
    async def test_fetch_events_apl_includes_env_filter(
        self, respx_mock
    ) -> None:
        captured: dict[str, str] = {}

        def _capture(request):
            import json

            body = json.loads(request.content)
            captured["apl"] = body.get("apl", "")
            return httpx.Response(200, json={"tables": []})

        respx_mock.post("/v1/datasets/_apl").mock(side_effect=_capture)

        adapter = AxiomReplayQueryAdapter(
            api_key="axk_test",
            dataset="bfx-funding-bot-shadow",
            deployment_environment=DeploymentEnvironment.SHADOW,
        )
        await adapter.fetch_events(event_types=["order_fill"])
        assert 'deployment_environment == "shadow"' in captured["apl"]
```

Ensure imports at top of the test file include:

```python
import httpx
import pytest
from datetime import UTC, datetime

from bfx_funding_bot.modules.execution.axiom_event_query import AxiomReplayQueryAdapter
from bfx_funding_bot.modules.observability.resource import DeploymentEnvironment
```

(Some may already be imported; only add what's missing.)

Also update existing test instantiations of `AxiomReplayQueryAdapter(...)` in that file — add `deployment_environment=DeploymentEnvironment.CI` parameter so they don't break. Use ci as default test value.

- [ ] **Step 3: Run extended test file**

```bash
cd backend_py && uv run pytest tests/modules/execution/test_axiom_event_query.py -v
```

Expected: New tests FAIL (`__init__` doesn't accept `deployment_environment`); existing tests may also fail if not patched.

- [ ] **Step 4: Modify `AxiomReplayQueryAdapter.__init__` + APL builder**

Edit `backend_py/src/bfx_funding_bot/modules/execution/axiom_event_query.py`:

Add import:

```python
from bfx_funding_bot.modules.observability.resource import DeploymentEnvironment
```

Modify `__init__` (lines 24-38):

```python
class AxiomReplayQueryAdapter:
    def __init__(
        self,
        *,
        api_key: str,
        dataset: str,
        deployment_environment: DeploymentEnvironment,
        base_url: str = "https://api.axiom.co",
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._dataset = dataset
        self._deployment_environment = deployment_environment
        # ...rest of existing init body unchanged...
```

Modify the APL builder method (find the method that constructs the APL query — call it `_build_apl` if it exists, otherwise modify the inline APL strings in `query_order_events` / `fetch_events`). Add a `where` clause:

For `query_order_events` APL:
```
| where deployment_environment == "{self._deployment_environment.value}"
```
inserted after `| where _time > datetime(...)` and before `| where account_id == ...`.

For `fetch_events` APL (similar location).

- [ ] **Step 5: Run extended test**

```bash
cd backend_py && uv run pytest tests/modules/execution/test_axiom_event_query.py -v
```

Expected: All tests pass (existing + new).

- [ ] **Step 6: Type-check + lint**

```bash
cd backend_py && uv run mypy --strict src/bfx_funding_bot/modules/execution/axiom_event_query.py && uv run ruff check src/bfx_funding_bot/modules/execution/axiom_event_query.py tests/modules/execution/test_axiom_event_query.py
```

Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/axiom_event_query.py backend_py/tests/modules/execution/test_axiom_event_query.py
git commit -m "✨ Feat: AxiomReplayQueryAdapter filters by deployment_environment (T12 spec D3)"
```

---

## Task 5: daemon.py `build_daemon` wiring — two-end symmetry

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py` (`build_daemon` function, search for it)
- Create: `backend_py/tests/modules/marketfeed/test_daemon_axiom_wiring.py`

- [ ] **Step 1: Locate `build_daemon`**

```bash
cd backend_py && grep -n "def build_daemon\|AxiomConfig\|AxiomClient\|AxiomReplayQueryAdapter" src/bfx_funding_bot/modules/marketfeed/daemon.py
```

Note the line numbers for each instantiation. We expect:
- `AxiomConfig(...)` or `AxiomConfig.from_env()` call
- `AxiomClient(cfg, ...)` instantiation
- `AxiomReplayQueryAdapter(...)` instantiation

- [ ] **Step 2: Write failing wiring test**

Create `backend_py/tests/modules/marketfeed/test_daemon_axiom_wiring.py`:

```python
"""Test daemon.build_daemon wires the SAME deployment_env into emit + replay paths.

Invariant: AxiomClient.resource.deployment_environment == AxiomReplayQueryAdapter._deployment_environment.
If these drift, daemon emits to one env but queries another — silent replay miss.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from bfx_funding_bot.modules.observability.resource import DeploymentEnvironment


@pytest.mark.parametrize("env_value", ["prod", "shadow", "ci"])
def test_build_daemon_emit_and_query_env_symmetric(
    env_value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build_daemon must wire client.resource.env == replay_adapter._deployment_environment."""
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", env_value)
    monkeypatch.setenv("AXIOM_API_KEY", "axk_test")
    monkeypatch.setenv("AXIOM_DATASET", f"bfx-funding-bot-{env_value}")
    # other env vars build_daemon needs — set to safe test values
    monkeypatch.setenv("BFX_SERVICE_VERSION", "test-sha")
    # ... add any other required env vars based on grep findings in step 1

    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon

    # Call build_daemon with minimum args — adapt signature based on actual impl
    daemon = build_daemon()  # adjust if build_daemon takes config

    # Pull the wired AxiomClient + replay adapter from daemon
    # Field names depend on daemon impl — adapt based on grep
    client_env = daemon.axiom_client._resource.deployment_environment
    adapter_env = daemon.axiom_replay_adapter._deployment_environment

    assert client_env is adapter_env
    assert client_env.value == env_value
```

Note: this test's exact form depends on how `build_daemon` constructs and exposes the daemon object. Adapt field access (`daemon.axiom_client` vs `daemon._client`, etc.) to match the actual impl after Step 1 grep. The invariant to lock is what matters: same `DeploymentEnvironment` value on both sides.

- [ ] **Step 3: Run test to verify it fails**

```bash
cd backend_py && uv run pytest tests/modules/marketfeed/test_daemon_axiom_wiring.py -v
```

Expected: FAIL — either build_daemon doesn't wire EventResource yet, or AxiomConfig instantiation lacks deployment_env, or assertion fires.

- [ ] **Step 4: Modify `build_daemon`**

Edit `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py`. Find the section that constructs `AxiomConfig`, `AxiomClient`, `AxiomReplayQueryAdapter`. Modify:

```python
from bfx_funding_bot.modules.observability.resource import (
    DeploymentEnvironment,
    EventResource,
)

def build_daemon(...) -> Daemon:
    # ... existing setup ...

    # Build AxiomConfig from env (Task 2 added .from_env())
    axiom_cfg = AxiomConfig.from_env()

    # Build process-level EventResource — emit + query share this env
    event_resource = EventResource(
        deployment_environment=axiom_cfg.deployment_env,
    )

    axiom_client = AxiomClient(cfg=axiom_cfg, resource=event_resource)

    axiom_replay_adapter = AxiomReplayQueryAdapter(
        api_key=axiom_cfg.api_key,
        dataset=axiom_cfg.dataset,
        deployment_environment=axiom_cfg.deployment_env,
    )

    # ... rest of build_daemon — wire client + adapter into daemon ...
```

The exact integration point depends on existing daemon structure. Replace the existing `AxiomConfig(...)` direct instantiation if any with `AxiomConfig.from_env()`.

- [ ] **Step 5: Run wiring test**

```bash
cd backend_py && uv run pytest tests/modules/marketfeed/test_daemon_axiom_wiring.py -v
```

Expected: All 3 parametrized cases PASS.

- [ ] **Step 6: Run full unit test suite**

```bash
cd backend_py && uv run pytest -m "not integration" -q
```

Expected: All PASS. If existing daemon tests fail, they may need monkey-patching `BFX_DEPLOYMENT_ENV` env var.

- [ ] **Step 7: Type-check + lint**

```bash
cd backend_py && uv run mypy --strict src/bfx_funding_bot/modules/marketfeed/daemon.py && uv run ruff check src/bfx_funding_bot/modules/marketfeed/daemon.py tests/modules/marketfeed/test_daemon_axiom_wiring.py
```

Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py backend_py/tests/modules/marketfeed/test_daemon_axiom_wiring.py
# stage any other test patches required by env binding
git commit -m "✨ Feat: build_daemon wires EventResource symmetric to query adapter (T12 spec D3+invariant)"
```

---

## Task 6: Dockerfile — propagate $GIT_SHA as BFX_SERVICE_VERSION

**Files:**
- Modify: `backend_py/Dockerfile`

- [ ] **Step 1: Inspect current Dockerfile**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot && cat backend_py/Dockerfile | head -40
```

Note where `ENV` declarations are. Add `ARG` declarations before the first `ENV` that needs them.

- [ ] **Step 2: Modify Dockerfile**

Edit `backend_py/Dockerfile`. Add near the top of the build stage (before the first `RUN` or `COPY` step that would invalidate cache on every build):

```dockerfile
ARG GIT_SHA=unknown
ENV BFX_SERVICE_VERSION=${GIT_SHA}
```

If Dockerfile uses multi-stage builds, add `ARG GIT_SHA=unknown` in EACH stage that needs it; only the final stage needs the `ENV` line (since runtime uses it).

- [ ] **Step 3: Verify build still works locally (optional but recommended)**

```bash
cd backend_py && docker build --build-arg GIT_SHA=$(git rev-parse --short HEAD) -t bfx-test .
```

Expected: build succeeds. (If docker not available locally, defer to CI verification.)

- [ ] **Step 4: Note Koyeb config update needed (no code change)**

Document in `backend_py/docs/runbooks/observability-env-setup.md` (created in Task 11): Koyeb build pipeline needs `--build-arg GIT_SHA=$KOYEB_GIT_SHA` (Koyeb provides `$KOYEB_GIT_COMMIT` env var; check Koyeb docs for exact var name).

- [ ] **Step 5: Commit**

```bash
git add backend_py/Dockerfile
git commit -m "🚀 Deploy: Dockerfile ARG GIT_SHA → ENV BFX_SERVICE_VERSION (T12 spec D4)"
```

---

## Task 7: T12 integration test upgrade — polling + env asserts

**Files:**
- Modify: `backend_py/tests/integration/test_axiom_replay_query_real.py`

- [ ] **Step 1: Read current T12 file**

```bash
cd backend_py && cat tests/integration/test_axiom_replay_query_real.py
```

Confirm: current shape uses `time.sleep(5)`, fixed `account_id`, no env/schema_version asserts.

- [ ] **Step 2: Replace T12 with upgraded version**

Rewrite `backend_py/tests/integration/test_axiom_replay_query_real.py`:

```python
"""T12 — AxiomReplayQueryAdapter round-trip integration test.

Emits 3 events to real Axiom CI dataset, polls until indexed, asserts
adapter parses them back correctly with deployment_environment/schema_version
envelope fields intact. Cross-run isolation via GITHUB_RUN_ID-scoped account_id.

Skip unless AXIOM_API_KEY + AXIOM_DATASET set in env (CI sets them via
backend_py_integration job; locally set via .env runbook).
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from bfx_funding_bot.external.axiom import AxiomClient, AxiomConfig
from bfx_funding_bot.modules.execution.axiom_event_query import AxiomReplayQueryAdapter
from bfx_funding_bot.modules.observability.resource import (
    DeploymentEnvironment,
    EventResource,
)

_AXIOM_AVAIL = bool(os.environ.get("AXIOM_API_KEY")) and bool(
    os.environ.get("AXIOM_DATASET")
)


async def _poll_until_indexed(
    adapter: AxiomReplayQueryAdapter,
    account_id: str,
    since: datetime,
    *,
    expected_count: int,
    timeout_s: float = 30.0,
    interval_s: float = 1.0,
) -> list[dict]:
    """Poll Axiom every interval_s until expected_count events visible or timeout.

    Replaces fixed time.sleep(5) — Axiom indexing latency varies 2-15s under load.
    """
    deadline = time.monotonic() + timeout_s
    rows: list[dict] = []
    while time.monotonic() < deadline:
        rows = await adapter.query_order_events(account_id=account_id, since=since)
        if len(rows) >= expected_count:
            return rows
        await asyncio.sleep(interval_s)
    raise AssertionError(
        f"Axiom indexing timeout: got {len(rows)}/{expected_count} after {timeout_s}s "
        f"(account_id={account_id})"
    )


@pytest.mark.integration
@pytest.mark.skipif(not _AXIOM_AVAIL, reason="AXIOM creds not set")
@pytest.mark.asyncio
async def test_axiom_replay_round_trip() -> None:
    # Run-scoped isolation — parallel CI runs and local runs don't cross-pollute
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    sha = os.environ.get("GITHUB_SHA", "local")[:8]
    run_account_id = f"ci-{run_id}-{sha}"
    test_start = datetime.now(UTC)

    cfg = AxiomConfig(
        api_key=os.environ["AXIOM_API_KEY"],
        dataset=os.environ["AXIOM_DATASET"],
        deployment_env=DeploymentEnvironment.CI,
        flush_interval_s=0.1,  # fast flush for test
    )
    resource = EventResource(
        deployment_environment=DeploymentEnvironment.CI,
        service_version=os.environ.get("BFX_SERVICE_VERSION", "test"),
    )
    client = AxiomClient(cfg=cfg, resource=resource)
    adapter = AxiomReplayQueryAdapter(
        api_key=cfg.api_key,
        dataset=cfg.dataset,
        deployment_environment=DeploymentEnvironment.CI,
    )

    await client.start()
    try:
        # Emit 3 events with unique correlation_id per event
        cid1, cid2, cid3 = str(uuid4()), str(uuid4()), str(uuid4())
        await client.emit(
            {
                "_time": int(test_start.timestamp() * 1000),
                "type": "reservation_claimed",
                "account_id": run_account_id,
                "payload": {
                    "cid": 1,
                    "venue_offer_id": "v1",
                    "size_usdt": 100.0,
                    "signal_correlation_id": cid1,
                    "is_simulated": True,
                },
            }
        )
        await client.emit(
            {
                "_time": int(test_start.timestamp() * 1000) + 1,
                "type": "order_fill",
                "account_id": run_account_id,
                "payload": {
                    "cid": 1,
                    "offer_id": "v1",
                    "signal_correlation_id": cid2,
                    "fill_size_usdt": 100.0,
                    "fill_price": 0.0001,
                    "is_simulated": True,
                },
            }
        )
        await client.emit(
            {
                "_time": int(test_start.timestamp() * 1000) + 2,
                "type": "reservation_released",
                "account_id": run_account_id,
                "payload": {
                    "cid": 1,
                    "venue_offer_id": "v1",
                    "size_usdt": 100.0,
                    "reason": "venue_cancel",
                    "signal_correlation_id": cid3,
                    "is_simulated": True,
                },
            }
        )
        await client.flush()
    finally:
        await client.stop()

    # Polling — replaces fixed sleep(5)
    rows = await _poll_until_indexed(
        adapter, run_account_id, test_start, expected_count=3, timeout_s=30
    )

    # Wire-level shape assertions
    types = {r["type"] for r in rows}
    assert types == {"reservation_claimed", "order_fill", "reservation_released"}

    # Envelope (resource) fields present
    assert all(r["deployment_environment"] == "ci" for r in rows), (
        f"expected all rows deployment_environment=ci, got: "
        f"{[r.get('deployment_environment') for r in rows]}"
    )
    assert all(r["schema_version"] == 1 for r in rows)
    assert all(r["service_name"] == "bfx-funding-bot" for r in rows)
    assert all(r["service_version"] for r in rows)  # non-empty

    # Payload re-nesting still works
    rc = next(r for r in rows if r["type"] == "reservation_claimed")
    assert rc["payload"]["cid"] == 1
    assert rc["payload"]["signal_correlation_id"] == cid1
```

- [ ] **Step 3: Verify integration test SKIPs cleanly when no creds**

```bash
cd backend_py && unset AXIOM_API_KEY AXIOM_DATASET && uv run pytest tests/integration/test_axiom_replay_query_real.py -v
```

Expected: `SKIPPED [1] reason: AXIOM creds not set`.

- [ ] **Step 4: (Optional) Run T12 locally if you have ci dataset creds**

If you've done Manual Cutover Steps 1-2 (Axiom CI dataset + token) and set `AXIOM_API_KEY` + `AXIOM_DATASET=bfx-funding-bot-ci` + `BFX_DEPLOYMENT_ENV=ci` locally:

```bash
cd backend_py && uv run pytest tests/integration/test_axiom_replay_query_real.py -v
```

Expected: PASS in ~5-15s (polling adaptive).

If creds not yet ready, defer verification to first CI run after Task 9 ships.

- [ ] **Step 5: Type-check + lint**

```bash
cd backend_py && uv run mypy --strict tests/integration/test_axiom_replay_query_real.py && uv run ruff check tests/integration/test_axiom_replay_query_real.py
```

Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add backend_py/tests/integration/test_axiom_replay_query_real.py
git commit -m "✅ Test: T12 upgrade — polling-based, run-scoped, env+schema_version asserts (spec D12+D13)"
```

---

## Task 8: xfail markers for 5 pre-existing integration failures

**Files:**
- Modify: `backend_py/tests/integration/test_daemon_shutdown.py` (2 tests confirmed, possibly 3rd)
- Modify: `backend_py/tests/integration/test_path_e_subprocess.py`
- Modify: `backend_py/tests/integration/test_warmup_determinism.py`

- [ ] **Step 1: Re-confirm exact failure count**

```bash
cd backend_py && uv run pytest tests/integration/test_daemon_shutdown.py tests/integration/test_path_e_subprocess.py tests/integration/test_warmup_determinism.py -v 2>&1 | tail -40
```

Note which tests fail. Spec claims "3 × test_daemon_shutdown" but earlier survey found only 2 functions — confirm here. If only 2, mark 2; if 3rd exists update spec note.

- [ ] **Step 2: Add `xfail(strict=False)` to each failing test**

For each failing test function, add the decorator just above the `def` line:

```python
@pytest.mark.xfail(
    strict=False,
    reason=(
        "Pre-existing infra mismatch (SQLite/JSONB or similar). "
        "Tracked in wiki/projects/bfx-funding-bot/index.md Pending '5 個 pre-existing "
        "integration failure 修'."
    ),
)
```

Apply to:
- `tests/integration/test_daemon_shutdown.py` — both `test_daemon_shutdown_happy_path` and `test_daemon_shutdown_does_not_hang_on_normal_stop` (and 3rd if present)
- `tests/integration/test_path_e_subprocess.py` — `test_path_e_subprocess_exits_with_auth_failed_code`
- `tests/integration/test_warmup_determinism.py` — `test_warmup_determinism`

Ensure `import pytest` is at top of each file (likely already present).

- [ ] **Step 3: Verify xfail markers behave correctly**

```bash
cd backend_py && uv run pytest tests/integration/test_daemon_shutdown.py tests/integration/test_path_e_subprocess.py tests/integration/test_warmup_determinism.py -v
```

Expected: tests show `XFAIL` (or `XPASS` if they unexpectedly pass; that's fine with `strict=False`). No `FAILED`.

- [ ] **Step 4: Commit**

```bash
git add backend_py/tests/integration/test_daemon_shutdown.py backend_py/tests/integration/test_path_e_subprocess.py backend_py/tests/integration/test_warmup_determinism.py
git commit -m "✅ Test: xfail(strict=False) pre-existing integration failures (T12 spec D15)"
```

---

## Task 9: CI workflow — backend_py_unit + backend_py_integration jobs

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Read current ci.yml**

```bash
cat /Users/will/second-brain/projects/startup/bfx-funding-bot/.github/workflows/ci.yml
```

Note: existing `backend` (Go, archived) + `frontend` (Next.js) jobs. Workflow-level header (`name:`, `on:`).

- [ ] **Step 2: Add concurrency + workflow-level permissions to top of file**

Edit `.github/workflows/ci.yml`. After the `on:` block, add (if not already present):

```yaml
concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

permissions:
  contents: read
```

If `concurrency:` block already exists, merge with the above expression for `cancel-in-progress`. If `permissions:` already exists, ensure `contents: read` is the only key (or narrower).

- [ ] **Step 3: Add `backend_py_unit` job**

Under `jobs:`, after existing jobs, append:

```yaml
  backend_py_unit:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: read
    defaults:
      run:
        working-directory: backend_py
    steps:
      # NOTE: Actions @v4/@v3 acceptable per OpenSSF transitional guidance.
      # Dependabot (.github/dependabot.yml) auto-bumps these to commit SHA via weekly PRs.
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true
      - run: uv sync --all-groups
      - run: uv run ruff check
      - run: uv run mypy --strict src
      - run: uv run pytest -m "not integration" -q
```

- [ ] **Step 4: Add `backend_py_integration` job**

After `backend_py_unit`, append:

```yaml
  backend_py_integration:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: read
    needs: backend_py_unit
    if: github.event.pull_request.head.repo.full_name == github.repository || github.event_name == 'push'
    defaults:
      run:
        working-directory: backend_py
    env:
      AXIOM_API_KEY: ${{ secrets.AXIOM_CI_API_KEY }}
      AXIOM_DATASET: ${{ secrets.AXIOM_CI_DATASET }}
      BFX_DEPLOYMENT_ENV: ci
      BFX_SERVICE_VERSION: ${{ github.sha }}
      GITHUB_RUN_ID: ${{ github.run_id }}
      GITHUB_SHA: ${{ github.sha }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true
      - run: uv sync --all-groups
      - run: uv run pytest -m integration -q --maxfail=3
```

- [ ] **Step 5: Validate YAML syntax**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot && uvx --from yamllint yamllint .github/workflows/ci.yml || python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"
```

Expected: no syntax error. (yamllint may flag style issues that are OK to ignore for now.)

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "👷 CI: add backend_py_unit + backend_py_integration jobs + hardening (T12 spec D8+D21)"
```

Note: this commit does NOT cause Axiom CI dataset to be exercised until Manual Cutover Step 6a (GitHub secrets) is complete. If pushed before secrets exist, `backend_py_integration` will fail. **Coordinate ordering**: complete Manual Cutover Steps 1, 2, 6a BEFORE pushing this commit, OR temporarily comment out the integration job until secrets are configured.

---

## Task 10: `.github/dependabot.yml`

**Files:**
- Create: `.github/dependabot.yml`

- [ ] **Step 1: Check if file exists**

```bash
test -f /Users/will/second-brain/projects/startup/bfx-funding-bot/.github/dependabot.yml && echo "EXISTS — will extend" || echo "MISSING — will create"
```

- [ ] **Step 2: Create or extend dependabot.yml**

If missing, create `.github/dependabot.yml`:

```yaml
version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
    open-pull-requests-limit: 5
    groups:
      actions:
        patterns:
          - "*"
```

If file exists, add the `github-actions` ecosystem entry under `updates:`.

- [ ] **Step 3: Commit**

```bash
git add .github/dependabot.yml
git commit -m "👷 CI: dependabot weekly Actions SHA pin auto-bump (T12 spec D21)"
```

---

## Task 11: Local dev runbook

**Files:**
- Create: `backend_py/docs/runbooks/observability-env-setup.md`

- [ ] **Step 1: Create runbook**

Create directory + file:

```bash
mkdir -p backend_py/docs/runbooks
```

Create `backend_py/docs/runbooks/observability-env-setup.md`:

```markdown
# Observability Env Setup Runbook

Spec: [`2026-05-23-axiom-ci-env-separation-design.md`](../superpowers/specs/2026-05-23-axiom-ci-env-separation-design.md)

## Three Axiom datasets

| Dataset | Use | Retention | Token env var |
|---|---|---|---|
| `bfx-funding-bot` | prod (Phase 4.4 canary) | 30d | `AXIOM_API_KEY` (prod scope) |
| `bfx-funding-bot-shadow` | Koyeb shadow service | 30d | `AXIOM_API_KEY` (shadow scope) |
| `bfx-funding-bot-ci` | CI integration + local dev | 3-7d | `AXIOM_CI_API_KEY` / `AXIOM_API_KEY` (ci scope) |

## Required env vars per env

| Var | prod | shadow | ci / local dev |
|---|---|---|---|
| `AXIOM_API_KEY` | prod token | shadow token | ci token |
| `AXIOM_DATASET` | `bfx-funding-bot` | `bfx-funding-bot-shadow` | `bfx-funding-bot-ci` |
| `BFX_DEPLOYMENT_ENV` | `prod` | `shadow` | `ci` |
| `BFX_SERVICE_VERSION` | `$GIT_SHA` (Koyeb build arg) | `$GIT_SHA` | `$GIT_SHA` or auto from `git rev-parse HEAD` |

## Local dev `.env` example

```env
# Local integration test against Axiom ci dataset
AXIOM_API_KEY=axk_xxx_ci_scoped_token
AXIOM_DATASET=bfx-funding-bot-ci
BFX_DEPLOYMENT_ENV=ci
BFX_SERVICE_VERSION=  # leave empty — auto-resolves via git
```

Then run integration tests locally:

```bash
cd backend_py && uv run pytest -m integration -v
```

## CI configuration

GitHub Actions repo secrets (Settings → Secrets and variables → Actions):

- `AXIOM_CI_API_KEY` = ci-dataset-scoped Axiom token
- `AXIOM_CI_DATASET` = `bfx-funding-bot-ci`

`.github/workflows/ci.yml` `backend_py_integration` job wires these into env automatically.

## Koyeb shadow service migration

1. Axiom console → create `bfx-funding-bot-shadow` dataset + generate dataset-scoped token (shadow scope)
2. Koyeb console → marketfeed shadow service → Edit env vars:
   - `AXIOM_DATASET` → `bfx-funding-bot-shadow`
   - `AXIOM_API_KEY` → `<shadow_scope_token>`
   - `BFX_DEPLOYMENT_ENV` → `shadow`
3. Save → Koyeb auto-redeploys
4. Verify (5-15 min observe window):
   - Koyeb logs: `AxiomConfig(deployment_env=DeploymentEnvironment.SHADOW)` on boot
   - Axiom console: `bfx-funding-bot-shadow` dataset receives events
   - Axiom console: `bfx-funding-bot` dataset stops receiving events
5. Rollback if needed: Koyeb console → env vars → revert to prod dataset values

## Phase 4.4 canary cutover (future)

1. Verify `bfx-funding-bot` dataset query `where _time > ago(30d) and deployment_environment != "prod"` returns 0 rows (30d after Koyeb shadow cutover)
2. GitHub Settings → Branches → `main` → require status check `backend_py_integration`
3. Koyeb prod service env:
   - `AXIOM_DATASET` → `bfx-funding-bot`
   - `AXIOM_API_KEY` → `<prod_scope_token>`
   - `BFX_DEPLOYMENT_ENV` → `prod`
   - `BFX_EXECUTOR` → `bitfinex_live`
   - `BFX_WS_CLIENT_ENABLED` → `true`

## Troubleshooting

- **`ValueError: BFX_DEPLOYMENT_ENV required`** — env var unset; export it
- **`ValueError: 'staging' is not a valid DeploymentEnvironment`** — only prod/shadow/ci valid
- **T12 `Axiom indexing timeout: got 0/3 after 30s`** — check (a) CI dataset token write permission, (b) Axiom service status page, (c) APL where clause matches GitHub_RUN_ID
- **CI fork PR integration job not running** — by design (`if:` clause); fork PRs cannot access secrets per GitHub security model
```

- [ ] **Step 2: Commit**

```bash
git add backend_py/docs/runbooks/observability-env-setup.md
git commit -m "📝 Docs: observability env setup runbook (T12 spec)"
```

---

## Final Verification

After all 11 tasks committed:

- [ ] **Run full unit test suite**

```bash
cd backend_py && uv run pytest -m "not integration" -q
```

Expected: ALL PASS, no skips beyond known acceptable patterns.

- [ ] **Run mypy + ruff full sweep**

```bash
cd backend_py && uv run mypy --strict src && uv run ruff check src tests
```

Expected: clean.

- [ ] **Integration test suite expected state**

```bash
cd backend_py && uv run pytest -m integration -v --no-header 2>&1 | tail -20
```

Expected:
- T12 (`test_axiom_replay_round_trip`) — SKIPPED unless AXIOM creds set locally (PASS in CI after Manual Cutover Steps complete)
- 5 pre-existing failures — XFAIL (not FAILED)
- Any other existing integration tests — PASS or as expected

- [ ] **Final commit (if anything missed)**

If any catch-up is needed, commit and push. Otherwise, plan execution is code-complete.

---

## Manual Cutover Steps (humans-only, not Claude-executable)

After plan execution complete, follow these in order. Each step has its own verify.

| Step | Action | Verify |
|---|---|---|
| 1 | Axiom console → create `bfx-funding-bot-shadow` (retention 30d) + `bfx-funding-bot-ci` (retention 3-7d) datasets | Datasets visible in Axiom console |
| 2 | Axiom console → generate 2 dataset-scoped API tokens (shadow + ci) | Tokens visible in Axiom token list |
| 3 | Push all commits from Tasks 1-11 to `main` | Koyeb auto-redeploys; deploy log shows `AxiomConfig(deployment_env=...)` |
| 4 | Koyeb shadow service env update: `AXIOM_DATASET=bfx-funding-bot-shadow` + `AXIOM_API_KEY=<shadow_token>` + `BFX_DEPLOYMENT_ENV=shadow` | Koyeb redeploy log shows new env values |
| 5 | Wait 5-15 min observe window | Axiom shadow dataset receiving events; prod dataset stopped receiving from shadow service |
| 6a | GitHub Settings → Secrets → add `AXIOM_CI_API_KEY` + `AXIOM_CI_DATASET=bfx-funding-bot-ci` | Secrets visible in repo Settings (values hidden) |
| 6b | First PR push or main push after step 6a triggers `backend_py_integration` job | Job goes green; Axiom ci dataset receives test events |
| 7 | Update local `.env` per runbook | Local `uv run pytest -m integration` passes |
| 8 | Update Phase 4.4 cutover prework checklist in `wiki/projects/bfx-funding-bot/index.md` Pending section | Checklist now includes: (a) `backend_py_integration` required check upgrade, (b) verify prod dataset 30d clean |
| 9 | After ship, run `/project-decision-log` to compress this spec → ADR in second-brain | ADR file at `wiki/projects/bfx-funding-bot/decisions/<spec ship date>-observability-env-separation.md` |

---

## Self-Review Notes

- Coverage: All 21 spec decisions (D1-D21) addressed across Tasks 1-11 + Manual Cutover.
- Goals: G1 (catch wire-level bugs) via Tasks 7+9; G2 (CI feedback latency) via Task 9 timeouts; G3 (isolation) via Task 7 run-scoped account_id; G4 (env purity) via Manual Cutover; G5 (OTEL readiness) via Task 1; G6 (schema evolution) via Task 1+7; G7 (CI security) via Task 9+10.
- Non-goals respected: no Pydantic `extra='ignore'`, no fixing pre-existing failures (only xfail), no full OTEL SDK adoption.
- Type consistency: `DeploymentEnvironment` StrEnum used in `AxiomConfig.deployment_env`, `AxiomClient` via `EventResource`, `AxiomReplayQueryAdapter._deployment_environment`, and T12 — all same import path.
- All file paths are absolute or anchored at `backend_py/`; all commands use `cd backend_py` per CLAUDE.md.
