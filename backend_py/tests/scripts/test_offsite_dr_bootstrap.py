"""Static contracts for the VM-only offsite DR bootstrap scripts."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WIZARD_PATH = ROOT / "scripts/setup-pgbackrest-r2.sh"
INSTALLER_PATH = ROOT / "scripts/install-pgbackrest-timers.sh"
EXPECTED_UNITS = (
    "bfx-pgbackrest-backup.service",
    "bfx-pgbackrest-backup.timer",
    "bfx-pgbackrest-status.service",
    "bfx-pgbackrest-status.timer",
)

wizard_text = WIZARD_PATH.read_text(encoding="utf-8")
installer_text = INSTALLER_PATH.read_text(encoding="utf-8")


def test_secret_wizard_captures_only_vm_secret_fragment() -> None:
    """The wizard stores only the approved fragment under the VM boundary."""
    stage_text = wizard_text.split("# STAGES", 1)[1]

    assert "# STAGES" in wizard_text
    assert 'ENV_FILE="$SECRET_FILE"' in stage_text
    assert "ask_secret R2_SECRET_ACCESS_KEY" in stage_text
    assert "ask_secret R2_CIPHER_PASS" in stage_text
    for option in (
        "repo1-s3-endpoint",
        "repo1-s3-bucket",
        "repo1-s3-key",
        "repo1-s3-key-secret",
        "repo1-cipher-pass",
    ):
        assert option in stage_text
    assert 'secret_validation.py" --secret-dir' in stage_text
    assert "install-pgbackrest-timers.sh" in stage_text
    assert 'sudo chown 70:70 "$SECRET_DIR" "$SECRET_FILE"' in stage_text
    assert "chmod 0750" in stage_text
    assert "chmod 0640" in stage_text
    assert "https://developers.cloudflare.com/r2/api/tokens/" in stage_text
    assert "set_secret " not in stage_text
    assert "BFX_API_SECRET" not in stage_text
    assert ".env" not in stage_text
    assert "bot.env" not in stage_text
    assert "webapi.env" not in stage_text
    assert "frontend.env" not in stage_text


def test_timer_installer_is_definition_only() -> None:
    """Installer loads definitions but preserves all existing timer state."""
    for unit_name in EXPECTED_UNITS:
        assert unit_name in installer_text
    assert "/etc/systemd/system" in installer_text
    assert "systemctl daemon-reload" in installer_text
    assert "systemctl is-active" in installer_text
    assert "systemctl is-enabled" in installer_text
    assert "systemctl enable" not in installer_text
    assert "systemctl start" not in installer_text
    assert "enable --now" not in installer_text
    assert "docker" not in installer_text.lower()
    assert '"pgbackrest"' not in installer_text.lower()
