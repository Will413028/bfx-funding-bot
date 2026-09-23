"""Integration test for _g3_loaders.build_verdict_from_neon.

Requires a live Neon connection (BFX_DEPLOYMENT_ENV + DATABASE_URL set).
Skipped by the commit gate (pytest -m "not integration").
"""
import os
from decimal import Decimal

import pytest


@pytest.mark.integration
async def test_build_verdict_from_neon_returns_g3_verdict():
    """build_verdict_from_neon must return without raising, regardless of data state.

    The canary may have zero fills (idle) — the loader must handle that gracefully
    and return an INSUFFICIENT_DATA verdict rather than raising.
    """
    if not os.environ.get("DATABASE_URL") or not os.environ.get("BFX_DEPLOYMENT_ENV"):
        pytest.skip("Neon integration requires DATABASE_URL and BFX_DEPLOYMENT_ENV")

    from bfx_funding_bot.modules.live_validation.live_attribution import G3Verdict
    from scripts._g3_loaders import build_verdict_from_neon

    verdict, data_window, n_fills, *_ = await build_verdict_from_neon(capital=Decimal("570"))

    assert isinstance(verdict, G3Verdict)
    assert isinstance(data_window, str)
    assert isinstance(n_fills, int)
    assert n_fills >= 0
