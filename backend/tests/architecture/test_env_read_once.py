"""The deployment environment is read by assembly points only (plan 2026-11-08, D4).

``core/settings.py`` owns the two readers; ``apps`` and each script's ``main`` call them
once and pass the value down. ``modules`` and the rest of ``core`` neither name the
variable nor call a reader, so no code path can pick up a second, different value.

Mutation check: call ``require_deployment_environment()`` again in
``modules/execution/operator_commands.py``.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "bfx_funding_bot"
READ = re.compile(r"BFX_DEPLOYMENT_ENV|require_deployment_environment|deployment_environment_label")


def test_modules_and_core_do_not_read_the_deployment_environment() -> None:
    readers = sorted({
        str(path.relative_to(SRC))
        for layer in ("modules", "core") for path in (SRC / layer).rglob("*.py")
        if READ.search(path.read_text())
    })
    assert readers == ["core/settings.py"]
