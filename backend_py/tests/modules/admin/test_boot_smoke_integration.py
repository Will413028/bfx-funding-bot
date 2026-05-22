"""Boot-time smoke wiring — verify _run() invokes smoke_runner.run_l2()
and continues on failure."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from bfx_funding_bot.modules.admin.smoke_runner import SmokeResult


async def test_boot_smoke_passes_logs_info(monkeypatch, caplog) -> None:
    """A passing boot smoke should log smoke_boot_passed."""
    import logging
    caplog.set_level(logging.INFO)

    # Build a minimal fake Daemon-like object
    daemon_stub = MagicMock()
    daemon_stub.smoke_runner = MagicMock()
    daemon_stub.smoke_runner.run_l2 = AsyncMock(return_value=SmokeResult(
        status="pass", level="L2", checks={}, duration_ms=10,
    ))
    daemon_stub.axiom = MagicMock()
    daemon_stub.axiom.emit = AsyncMock()

    from bfx_funding_bot.modules.marketfeed.daemon_smoke_boot import run_boot_smoke
    await run_boot_smoke(daemon_stub)

    daemon_stub.smoke_runner.run_l2.assert_awaited_once()
    daemon_stub.axiom.emit.assert_not_awaited()
    assert any("smoke_boot_passed" in rec.message for rec in caplog.records)


async def test_boot_smoke_fails_emits_safety_trigger(caplog) -> None:
    import logging
    caplog.set_level(logging.CRITICAL)

    daemon_stub = MagicMock()
    daemon_stub.smoke_runner = MagicMock()
    daemon_stub.smoke_runner.run_l2 = AsyncMock(return_value=SmokeResult(
        status="fail", level="L2", checks={"events_count": 1},
        duration_ms=5, error="expected 2 events, got 1",
    ))
    daemon_stub.axiom = MagicMock()
    daemon_stub.axiom.emit = AsyncMock()
    daemon_stub.config.phase.value = "paper"

    from bfx_funding_bot.modules.marketfeed.daemon_smoke_boot import run_boot_smoke
    await run_boot_smoke(daemon_stub)

    daemon_stub.axiom.emit.assert_awaited_once()
    emit_call = daemon_stub.axiom.emit.call_args[0][0]
    assert emit_call["event_type"] == "safety_trigger"
    assert emit_call["level"] == "critical"
    assert "smoke_boot" in str(emit_call["payload"])


async def test_boot_smoke_none_runner_skips_silently(caplog) -> None:
    """If smoke_runner is None on Daemon (e.g. wiring opt-out), block is no-op."""
    daemon_stub = MagicMock()
    daemon_stub.smoke_runner = None
    daemon_stub.axiom = MagicMock()
    daemon_stub.axiom.emit = AsyncMock()

    from bfx_funding_bot.modules.marketfeed.daemon_smoke_boot import run_boot_smoke
    await run_boot_smoke(daemon_stub)

    daemon_stub.axiom.emit.assert_not_awaited()
