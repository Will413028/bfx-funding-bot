"""The simulated venue is composed in one place, and only there.

Mutations: import ``simulated_venue.wiring`` from another module (this test and the
``simulated-venue-wiring-is-top`` import contract fail); import any ``simulated_venue``
facade from a runtime module.
"""
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
SRC = BACKEND / "src" / "bfx_funding_bot"
WIRING = re.compile(r"^\s*(from\s+bfx_funding_bot\.modules\.simulated_venue\.wiring\b|"
                    r"from\s+bfx_funding_bot\.modules\.simulated_venue\s+import[^\n]*\bwiring\b)",
                    re.MULTILINE)
FACADE = re.compile(r"^\s*(from|import)\s+bfx_funding_bot\.modules\.simulated_venue\b", re.MULTILINE)


def _importers(pattern: re.Pattern[str], root: Path) -> set[str]:
    return {
        str(path.relative_to(BACKEND)) for path in root.rglob("*.py")
        if pattern.search(path.read_text())
    }


def test_only_apps_venue_imports_the_simulated_venue_wiring() -> None:
    assert _importers(WIRING, SRC) == {"src/bfx_funding_bot/apps/venue.py"}


def test_only_apps_and_the_module_itself_import_the_simulated_venue() -> None:
    outside = {p for p in _importers(FACADE, SRC)
               if "/modules/simulated_venue/" not in p}
    # apps/schema.py only registers the venue's table with Base.metadata.
    assert outside == {"src/bfx_funding_bot/apps/venue.py", "src/bfx_funding_bot/apps/schema.py"}
