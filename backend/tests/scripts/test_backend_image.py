import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "backend/Dockerfile"


def stages(text: str) -> dict[str, str]:
    """Each stage's instructions (comments dropped), keyed by its `AS` name, in order."""
    text = re.sub(r"^\s*#.*$", "", text, flags=re.MULTILINE)
    parts = re.split(r"^FROM\s+\S+\s+AS\s+(\S+)\s*$", text, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_the_runtime_stage_is_last_and_installs_nothing() -> None:
    """The production image is the last stage; the compiler and uv stay in the builder."""
    found = stages(DOCKERFILE.read_text())
    assert list(found)[-1] == "runtime"
    runtime = found["runtime"]
    for tool in ("apt-get", "apk ", "build-essential", "gcc", "pip install", "uv sync", "uv=="):
        assert tool not in runtime, tool
    assert "COPY --from=build /app/.venv/ ./.venv/" in runtime
    assert "USER appuser" in runtime


def test_the_builder_and_the_runtime_share_one_base() -> None:
    """The venv's interpreter symlink resolves only on the base it was built on."""
    text = DOCKERFILE.read_text()
    bases = {
        name: image
        for image, name in re.findall(r"^FROM\s+(\S+)\s+AS\s+(\S+)\s*$", text, flags=re.MULTILINE)
    }
    assert bases["build"] == bases["runtime"]
    assert "@sha256:" in bases["runtime"]
