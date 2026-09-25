"""systemd units and installer for the VM deploy/alert/DR tooling.

The repository lesson: a unit run as root resolves `~` and an unset $HOME to
/root, so every unit states HOME explicitly and uses absolute paths only.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SYSTEMD = ROOT / "deploy/vm/systemd"
NEW_UNITS = ("bfx-deploy.service", "bfx-deploy.timer", "bfx-alert@.service",
             "bfx-backup-check.service", "bfx-backup-check.timer",
             "bfx-restore-test.service", "bfx-restore-test.timer")


class Unit(dict[str, dict[str, list[str]]]):
    """systemd keys may repeat (Environment=); keep every value in order."""

    def one(self, section: str, key: str) -> str:
        values = self[section][key]
        assert len(values) == 1, (section, key, values)
        return values[0]


def _unit(name: str) -> Unit:
    unit = Unit()
    section = ""
    for raw in (SYSTEMD / name).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            unit.setdefault(section, {})
            continue
        key, _, value = line.partition("=")
        unit[section].setdefault(key, []).append(value)
    return unit


def test_installer_lists_exactly_the_units_it_needs() -> None:
    installer = (ROOT / "deploy/vm/ops/install.sh").read_text(encoding="utf-8")
    listed = set(re.findall(r"^\s+(bfx-[\w@.-]+\.(?:service|timer))$", installer, re.M))
    assert listed == {*NEW_UNITS, "bfx-pgbackrest-backup.service"}
    for tool in ("bfx_deploy.py", "bfx_notify.py", "bfx_backup_check.py", "bfx_restore_test.py",
                 "change_class.py"):
        assert tool in installer and (ROOT / "deploy/vm/ops" / tool).is_file()
    assert "systemctl enable" not in installer and "systemctl start" not in installer


@pytest.mark.parametrize("name", [n for n in NEW_UNITS if n.endswith(".service")])
def test_services_state_home_and_use_absolute_paths(name: str) -> None:
    unit = _unit(name)
    assert unit.one("Service", "Type") == "oneshot"
    assert re.fullmatch(r"[1-9][0-9]*[hms]?", unit.one("Service", "TimeoutStartSec"))
    user = unit["Service"].get("User", ["root"])[0]
    home = "/root" if user == "root" else f"/home/{user}"
    assert f"HOME={home}" in unit["Service"]["Environment"]
    exec_start = unit.one("Service", "ExecStart")
    assert exec_start.startswith("/usr/bin/python3 /usr/local/lib/bfx-ops/")
    assert "~" not in exec_start and "%h" not in exec_start and "$HOME" not in exec_start
    for token in exec_start.split():
        if "/" in token and not token.startswith('"'):
            assert token.startswith("/"), token


def test_units_run_as_the_principal_each_job_needs() -> None:
    assert "User" not in _unit("bfx-deploy.service")["Service"]           # docker + root-only secrets
    assert "User" not in _unit("bfx-backup-check.service")["Service"]     # root-only Telegram creds
    assert "User" not in _unit("bfx-alert@.service")["Service"]
    assert _unit("bfx-restore-test.service").one("Service", "User") == "ubuntu"  # the drill's evidence owner


def test_deploy_timer_every_five_minutes_offset_from_status() -> None:
    assert _unit("bfx-deploy.timer").one("Timer", "OnCalendar") == "*:2/5"
    assert _unit("bfx-backup-check.timer").one("Timer", "OnCalendar") == "*:1/5"
    assert _unit("bfx-pgbackrest-status.timer").one("Timer", "OnCalendar") == "*:0/5"
    # Monthly, six hours clear of the 03:17 UTC backup and Monday's 04:17 report.
    assert _unit("bfx-restore-test.timer").one("Timer", "OnCalendar") == "*-*-01 09:17:00 UTC"
    assert _unit("bfx-restore-test.timer").one("Timer", "Persistent") == "true"
    for timer in ("bfx-deploy.timer", "bfx-backup-check.timer", "bfx-restore-test.timer"):
        assert _unit(timer).one("Install", "WantedBy") == "timers.target"


def test_failures_that_cannot_alert_themselves_use_the_alert_template() -> None:
    assert _unit("bfx-restore-test.service").one("Unit", "OnFailure") == "bfx-alert@%n.service"
    assert _unit("bfx-pgbackrest-backup.service").one("Unit", "OnFailure") == "bfx-alert@%n.service"
    # bfx-deploy and bfx-backup-check alert on their own; an OnFailure would double-page.
    assert "OnFailure" not in _unit("bfx-deploy.service")["Unit"]
    assert "OnFailure" not in _unit("bfx-backup-check.service")["Unit"]
    alert = _unit("bfx-alert@.service").one("Service", "ExecStart")
    assert alert.startswith("/usr/bin/python3 /usr/local/lib/bfx-ops/bfx_notify.py --level critical")
    assert "%i" in alert


def test_restore_test_runs_the_existing_drill_from_the_operator_checkout() -> None:
    exec_start = _unit("bfx-restore-test.service").one("Service", "ExecStart")
    assert "--drill /home/ubuntu/bfx-funding-bot/deploy/vm/pgbackrest/restore-drill.sh" in exec_start
    assert "--heartbeat /home/ubuntu/bfx/dr-evidence/restore-heartbeat.json" in exec_start
    # Prefix receipts never overwrite the baseline drill's restore.json (Halt 2 reads it).
    assert "--evidence /home/ubuntu/bfx/dr-evidence/restore-prefix.json" in exec_start
    check = _unit("bfx-backup-check.service").one("Service", "ExecStart")
    assert "--evidence /home/ubuntu/bfx/dr-evidence/backup.json" in check
    assert "--restore-heartbeat /home/ubuntu/bfx/dr-evidence/restore-heartbeat.json" in check
    assert "--restore-max-age-seconds 3024000" in check  # 35 days for a monthly test


def test_deploy_service_never_gets_killed_mid_backup() -> None:
    unit = _unit("bfx-deploy.service")
    assert unit.one("Service", "TimeoutStartSec") == "6h"  # > backup + restore test + pulls + migration
    assert "Restart" not in unit["Service"]
    assert "--mirror /home/ubuntu/bfx-funding-bot" in unit.one("Service", "ExecStart")


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_installer_passes_shellcheck() -> None:
    completed = subprocess.run(["shellcheck", str(ROOT / "deploy/vm/ops/install.sh")],
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout
