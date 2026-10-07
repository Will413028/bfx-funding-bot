"""Property-test budgets come only from Hypothesis profiles (tests/conftest.py).

A test that pins ``max_examples`` keeps that count under every profile, so the
nightly 1k budget the `property` marker promises would silently skip it.
"""
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def test_no_test_pins_its_own_example_budget() -> None:
    pinned = [
        str(path.relative_to(TESTS)) for path in sorted(TESTS.rglob("*.py"))
        if path.name not in {"conftest.py", Path(__file__).name}
        and "max_examples" in path.read_text()
    ]
    assert pinned == []
