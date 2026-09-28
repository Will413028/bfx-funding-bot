"""Typing and import isolation are executable contracts, not runtime isinstance checks."""
import subprocess
import sys
from pathlib import Path


def test_static_protocol_conformance() -> None:
    fixture = Path(__file__).with_name("typing_contracts.py")
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", str(fixture)],
        cwd=fixture.parents[3], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_wiring_import_is_pure_in_fresh_process() -> None:
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
from bfx_funding_bot.modules.strategy import wiring
for module in sys.modules:
    assert not any(module == blocked or module.startswith(blocked + '.') for blocked in (
        'sqlalchemy', 'httpx', 'yaml', 'bfx_funding_bot.core.settings',
        'bfx_funding_bot.modules.candles.service', 'bfx_funding_bot.external.bitfinex.rest',
    )), module
"""], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
