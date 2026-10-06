"""Venue differences are decided in one place: ``build_venue`` and the wiring it returns.

No consumer compares a venue name or reads the venue off the config to branch; they read what
the wiring states (credentials, capabilities, tasks, ``aclose``). Mutation: compare
``venue == "..."`` (or ``config.venue``) in ``bot.py``, the registry or the daemon.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "bfx_funding_bot"
# The one place that may name a venue: the axis itself, and the wiring that implements it.
OWNERS = {"core/venue.py", "apps/venue.py", "apps/authority_support.py"}
COMPARES = re.compile(r"""(?<![.\w])venue\s*(==|!=|is|is not|in|not in)\s*[("'\[]|[("']bitfinex[)"']\s*==""")
ATTRIBUTE = re.compile(r"\b(config|cfg)\.venue\b")


def test_no_consumer_compares_a_venue_name_or_reads_config_venue() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC))
        if rel in OWNERS:
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            code = line.split("#", 1)[0]
            if COMPARES.search(code) or ATTRIBUTE.search(code):
                offenders.append(f"{rel}:{number}: {line.strip()}")
    # ``apps/bot.py`` passes ``config.venue`` to the seed rule (``apps/authority_support.py``);
    # nothing else may.
    allowed = ("require_ledger_seed",)
    assert [o for o in offenders
            if not o.startswith("apps/bot.py") or not any(a in o for a in allowed)] == []


def test_the_registry_and_the_daemon_take_capabilities_not_venue_strings() -> None:
    registry = (SRC / "modules/execution/registry.py").read_text()
    # The executor is the same on both venues; the composition root reads the capabilities.
    assert "core.venue" not in registry and "Venue," not in registry
    daemon = (SRC / "modules/marketfeed/daemon.py").read_text()
    assert "venue_tasks" in daemon and "venue_aclose" in daemon
    assert "venue_feed" not in daemon and "venue_client" not in daemon
