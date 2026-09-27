"""Integration test for _g3_loaders.build_verdict_from_neon.

Requires a live Postgres connection (BFX_DEPLOYMENT_ENV + DATABASE_URL set).
Skipped by the commit gate (pytest -m "not integration").
"""
import os
from decimal import Decimal

import pytest


@pytest.mark.integration
async def test_build_verdict_from_neon_returns_g3_report():
    """build_verdict_from_neon must return without raising, regardless of data state.

    The canary may have no attributed credit (idle) — the loader must handle
    that gracefully and return a verdict rather than raising. It reads the
    credit model tables (funding_credit_history, funding_trades,
    funding_interest_payments), so this also proves they exist at head.
    """
    if not os.environ.get("DATABASE_URL") or not os.environ.get("BFX_DEPLOYMENT_ENV"):
        pytest.skip("integration requires DATABASE_URL and BFX_DEPLOYMENT_ENV")

    from bfx_funding_bot.modules.live_validation.live_attribution import G3Report, G3Verdict
    from scripts._g3_loaders import build_verdict_from_neon

    report = await build_verdict_from_neon(capital=Decimal("570"))

    assert isinstance(report, G3Report)
    assert isinstance(report.verdict, G3Verdict)
    assert isinstance(report.data_window, str)
    assert report.coverage.bot_credits >= 0
    assert report.capital_source == "--capital 570"
    assert all(r.complete for r in report.reconciliations if r.flagged)
