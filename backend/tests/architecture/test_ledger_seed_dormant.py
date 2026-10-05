"""The legacy closure seed is dormant: nothing deployed can reach it (pre-flight §B).

Deploy, compose, systemd, the image, CI workflows and the package scripts never name it, and in
``src`` only the seed CLI imports the closure reader and the seed facade.

Mutation check: reference ``bfx_funding_bot.apps.ledger_seed`` from a systemd unit, the app
compose file or the bot composition root; each fails here.
"""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "backend" / "src" / "bfx_funding_bot"
NAME = re.compile(r"ledger[_-]seed")
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
    hits = sorted(
        path.relative_to(REPO).as_posix() for path in deployed
        if NAME.search(path.read_text(errors="replace"))
    )
    assert hits == []


def test_only_the_seed_cli_imports_the_seed() -> None:
    importers = sorted(
        path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
        if SEED_IMPORT.search(path.read_text())
    )
    assert importers == ["apps/ledger_seed.py"]
