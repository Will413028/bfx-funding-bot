"""The offline CLI must surface main-YAML load failures before fixture work."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/derive_cells.py"


@pytest.mark.parametrize(
    ("main_yaml", "error"),
    [
        (None, "FileNotFoundError: cells.yaml not found at configs/cells.yaml"),
        ("cells: [\n", "yaml.parser.ParserError:"),
    ],
)
def test_check_reports_main_yaml_load_error(
    tmp_path: Path, main_yaml: str | None, error: str,
) -> None:
    if main_yaml is not None:
        configs = tmp_path / "configs"
        configs.mkdir()
        (configs / "cells.yaml").write_text(main_yaml)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert error in result.stderr
    assert "cells.live.yaml" not in result.stderr


def test_check_reports_fixture_drift_before_reading_invalid_deployed_yaml(
    tmp_path: Path,
) -> None:
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "cells.yaml").write_text(
        "cells:\n"
        "  - strategy: mean_reversion\n"
        "    symbol: fUST\n"
        "    period_agg: a30\n"
        "    params: {threshold_sigma: 1.0, ratio_sigma: 0.5, ema_span: 168}\n"
        "_provenance: {data_hash: stale}\n"
    )
    (configs / "cells.live.yaml").write_text("cells: [\n")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "DRIFT: _provenance.data_hash 'stale' != on-disk fixture hash" in result.stderr
    assert "ParserError" not in result.stderr
