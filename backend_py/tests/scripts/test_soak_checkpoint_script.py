from pathlib import Path


SCRIPT = Path(__file__).parents[3] / "deploy" / "vm" / "soak-checkpoint.sh"


def _source() -> str:
    return SCRIPT.read_text()


def test_checkpoint_script_has_fail_closed_shell_and_absolute_report_targets():
    source = _source()

    assert "set -euo pipefail" in source
    assert 'REPORTS_DIR="/home/ubuntu/bfx/reports"' in source
    assert 'soak-checkpoint-latest.txt' in source
    assert 'soak-checkpoint-${STAMP}.txt' in source
    assert 'WINDOW="${1:-90m}"' in source
    assert 'mktemp /tmp/soak-checkpoint.' in source


def test_checkpoint_script_captures_logs_once_and_counts_idle_canary_safely():
    source = _source()

    assert source.count("docker logs --since") == 1
    assert "grep -cE" in source
    assert "|| true" in source
    assert 'docker logs --since "$WINDOW" bfx-bot > "$LOG_FILE"' in source


def test_checkpoint_script_probes_only_readonly_admin_and_health_paths():
    source = _source()

    assert "/healthz" in source
    assert "/readyz" in source
    assert "/admin/trading-status" in source
    assert "/admin/dry-evaluate" in source
    assert "/admin/halt" not in source
    assert "/admin/resume" not in source
    assert 'BFX_ADMIN_TOKEN' in source
    assert 'echo "$BFX_ADMIN_TOKEN"' not in source


def test_checkpoint_script_forbids_mutating_deployment_commands():
    source = _source()

    assert "git pull" not in source
    for command in ("docker compose up", "docker compose down", "docker compose restart"):
        assert command not in source


def test_checkpoint_script_queries_candle_and_reconcile_state_with_select_only():
    source = _source()

    assert "funding_candle_revisions" in source
    assert "finalized_at_ms" in source
    assert "reconcile_observation" in source
    assert 'readonly_sql "SELECT' in source
    assert "INSERT " not in source
    assert "UPDATE " not in source
    assert "DELETE " not in source
