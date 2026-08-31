"""Path E end-to-end — daemon subprocess exits 78 on ExecutorAuthError.

The in-process Path E coverage lives in tests/modules/execution/
test_integration_fatal.py (constants + ExecutorAuthError class). This file
adds the only test that actually exercises daemon._run's
`except* ExecutorAuthError → sys.exit(78)` end-to-end via a
real subprocess so we can observe the OS-level returncode.

Koyeb's crash-loop-backoff contract depends on the daemon exiting with
sysexits EX_CONFIG=78 (not 1, not -SIGKILL). In-process tests can't
validate sys.exit() because it would terminate pytest itself.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from bfx_funding_bot.core.errors import EXIT_CODE_AUTH_FAILED

pytestmark = pytest.mark.integration

_HARNESS = Path(__file__).resolve().parent / "_path_e_harness.py"
_CELLS_YAML = (
    Path(__file__).resolve().parents[2] / "configs" / "cells.yaml"
)


@pytest.mark.asyncio
async def test_path_e_subprocess_exits_with_auth_failed_code(
    pg_engine, pg_session_factory,
) -> None:
    assert _HARNESS.exists(), f"harness file not found: {_HARNESS}"
    assert _CELLS_YAML.exists(), f"cells.yaml not found: {_CELLS_YAML}"

    env = os.environ.copy()
    # render_as_string(hide_password=False) — SQLAlchemy URL.__str__ masks
    # the password as "***" by default; the subprocess needs the real one.
    env["DATABASE_URL"] = pg_engine.url.render_as_string(hide_password=False)
    env["BFX_CELLS_YAML"] = str(_CELLS_YAML)
    env["BFX_PHASE"] = "paper"
    env["BFX_DEPLOYMENT_ENV"] = "ci"
    env["BFX_EXECUTION_POLICY"] = "paper"
    env["BFX_API_KEY"] = "test_key"
    env["BFX_API_SECRET"] = "test_secret"
    # Port 0 → kernel-assigned random port; avoids collision when a real
    # bfx daemon is running locally on 8080.
    env["BFX_HEALTHZ_PORT"] = "0"

    result = subprocess.run(
        [sys.executable, str(_HARNESS)],
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == EXIT_CODE_AUTH_FAILED, (
        f"expected exit {EXIT_CODE_AUTH_FAILED}, got {result.returncode}\n"
        f"stdout:\n{result.stdout.decode(errors='replace')}\n"
        f"stderr:\n{result.stderr.decode(errors='replace')}"
    )
