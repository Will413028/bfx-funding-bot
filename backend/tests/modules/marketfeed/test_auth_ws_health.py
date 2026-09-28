"""Observe-only health assessment for the authenticated Bitfinex WS.

Catches the 2026-07 nonce bug's signature — connected but never authenticated —
which no disconnect callback can see (it's an ABSENCE of success). Pure function;
the daemon poll loop is thin glue over it.
"""
from bfx_funding_bot.core.health import assess_auth_ws_health
from bfx_funding_bot.core.telemetry import HealthStatus


def test_boot_grace_not_yet_connected_is_healthy():
    # connection_count 0 → hasn't tried yet; must not false-alarm at boot.
    s, msg = assess_auth_ws_health(
        connection_count=0, auth_ok_count=0, reconnect_count_last_hour=0,
    )
    assert s is HealthStatus.HEALTHY
    assert msg is None


def test_connected_but_never_authed_is_down():
    # THE bug signature: sockets open repeatedly, zero successful auths.
    s, msg = assess_auth_ws_health(
        connection_count=3, auth_ok_count=0, reconnect_count_last_hour=13,
    )
    assert s is HealthStatus.DOWN
    assert msg is not None and "authenticat" in msg.lower()


def test_authed_and_stable_is_healthy():
    s, msg = assess_auth_ws_health(
        connection_count=1, auth_ok_count=1, reconnect_count_last_hour=0,
    )
    assert s is HealthStatus.HEALTHY
    assert msg is None


def test_authed_but_flapping_is_degraded():
    s, msg = assess_auth_ws_health(
        connection_count=9, auth_ok_count=4, reconnect_count_last_hour=8,
    )
    assert s is HealthStatus.DEGRADED
    assert msg is not None and "flap" in msg.lower()
