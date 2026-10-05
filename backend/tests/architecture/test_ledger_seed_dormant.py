"""The legacy closure seed is dormant: nothing deployed can reach it (pre-flight §B).

Deploy, compose, systemd, the image, CI workflows and the package scripts never name it, and in
``src`` only the seed CLI imports the closure reader and the seed facade. The one exception is
the host switch tool (``deploy/vm/ops/bfx_ledger_switch.py``, switch pre-flight §F): Will runs
it by hand, once, as root; it starts the seed CLI in a one-shot container. No unit, timer,
compose file, wrapper or other tool names the switch tool, so nothing runs it automatically
(the transient timer the tool itself schedules after a switch runs only its read-only ``watch``).

Mutation check: reference ``bfx_funding_bot.apps.ledger_seed`` from a systemd unit, the app
compose file or the bot composition root; or ``bfx_ledger_switch`` from a timer, a unit or
``install.sh``; each fails here.
"""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "backend" / "src" / "bfx_funding_bot"
NAME = re.compile(r"ledger[_-]seed")
SWITCH_TOOL = "deploy/vm/ops/bfx_ledger_switch.py"
SWITCH_NAME = re.compile(r"ledger[_-]switch")
SEED_IMPORT = re.compile(
    r"(modules\.execution\.ledger_seed|modules\.execution import ledger_seed|"
    r"modules\.ledger\.seed\b|modules\.ledger import seed\b|apps\.ledger_seed|"
    r"apps import ledger_seed)"
)


def _tracked() -> list[Path]:
    listed = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"], check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    return [REPO / name for name in listed]


def _deployed(path: Path) -> bool:
    relative = path.relative_to(REPO).as_posix()
    return (
        relative.startswith(("deploy/", ".github/", "infra/"))
        or re.fullmatch(r"docker-compose[^/]*\.ya?ml", relative) is not None
        or relative.endswith("Dockerfile")
        or relative == "backend/pyproject.toml"
        or relative == "lefthook.yml"
    )


def test_no_deploy_compose_systemd_or_ci_file_names_the_seed() -> None:
    deployed = [path for path in _tracked() if _deployed(path) and path.is_file()]
    assert any(p.name == "docker-compose.app.yml" for p in deployed)  # the scan is not empty
    assert any(p.suffix == ".service" for p in deployed)
    assert any(p.relative_to(REPO).as_posix() == SWITCH_TOOL for p in deployed)
    hits = sorted(
        path.relative_to(REPO).as_posix() for path in deployed
        if NAME.search(path.read_text(errors="replace"))
    )
    assert hits == [SWITCH_TOOL]


def test_nothing_deployed_runs_the_switch_tool() -> None:
    """Only the tool itself names it: no unit, timer, compose file, wrapper or other tool."""
    deployed = [path for path in _tracked() if _deployed(path) and path.is_file()]
    hits = sorted(
        path.relative_to(REPO).as_posix() for path in deployed
        if SWITCH_NAME.search(path.read_text(errors="replace"))
    )
    assert hits == [SWITCH_TOOL]


def test_only_the_seed_cli_imports_the_seed() -> None:
    importers = sorted(
        path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
        if SEED_IMPORT.search(path.read_text())
    )
    assert importers == ["apps/ledger_seed.py"]
