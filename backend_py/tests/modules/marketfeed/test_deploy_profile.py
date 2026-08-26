"""Deployment profiles keep execution policies explicit and fail-closed."""
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def parse_env_file(path: Path) -> dict[str, str]:
    return {
        key: value
        for line in path.read_text().splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#") and "=" in stripped
        for key, value in [stripped.split("=", maxsplit=1)]
    }


def test_shadow_p14_is_shadow_only() -> None:
    env = parse_env_file(REPOSITORY_ROOT / "deploy/vm/shadow-p14.env")

    assert env["BFX_PHASE"] == "shadow"
    assert env["BFX_DEPLOYMENT_ENV"] == "shadow"
    assert env["BFX_CELLS_YAML"].endswith("cells.experimental-p14.yaml")
    assert env["BFX_EXECUTION_POLICY"] == "optimizer_shadow"


def test_deployment_profiles_contain_no_legacy_clamp_flags() -> None:
    for path in (REPOSITORY_ROOT / "deploy/vm").glob("*.env"):
        assert "BFX_CLAMP_" not in path.read_text()
