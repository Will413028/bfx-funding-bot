"""Host alerting and DR watch tools: bfx_notify, bfx_backup_check, bfx_restore_test.

Contracts: an alert never makes its caller fail and never logs the bot token;
the backup check alerts on RPO/staleness, a disabled restore timer, a stale
weekly report and disk usage once confirmed, de-duplicates and announces
recovery; the restore test refreshes its heartbeat only after the
existing drill succeeded with fresh measured evidence.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
OPS = ROOT / "deploy/vm/ops"
# A fake bot token, assembled at run time so no token-shaped literal sits in the
# source for secret scanners; same shape as a real one (digits:35 url-safe chars).
TOKEN = "1234" + "56789:" + "fake-" + "TelegramToken" + "x" * 17


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


notify = _load("vm_ops_notify_under_test", OPS / "bfx_notify.py")
check = _load("vm_ops_backup_check_under_test", OPS / "bfx_backup_check.py")
restore = _load("vm_ops_restore_test_under_test", OPS / "bfx_restore_test.py")


def _config(tmp_path: Path, text: str | None = None) -> Path:
    path = tmp_path / "notify.env"
    path.write_text(text if text is not None else f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=-1001234\n")
    return path


# --------------------------------------------------------------------------- notify


def test_notify_posts_the_message_to_the_configured_chat(tmp_path: Path) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    def transport(url: str, body: bytes, timeout: float) -> int:
        sent.append((url, json.loads(body)))
        return 200

    assert notify.send("deploy deployed", level="info", config_path=_config(tmp_path),
                       transport=transport, host="vm-1") is True
    url, payload = sent[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert payload["chat_id"] == "-1001234"
    assert payload["text"] == "[bfx][INFO][vm-1] deploy deployed"


@pytest.mark.parametrize("config_text", [None, "", "TELEGRAM_CHAT_ID=1\n", "garbage line\n",
                                         f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=abc def\n"])
def test_missing_or_broken_config_is_logged_and_never_raises(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], config_text: str | None,
) -> None:
    path = tmp_path / "absent.env" if config_text is None else _config(tmp_path, config_text)

    def transport(url: str, body: bytes, timeout: float) -> int:
        raise AssertionError("must not send without a valid config")

    assert notify.send("backup failed", config_path=path, transport=transport) is False
    assert "not delivered" in capsys.readouterr().err


@pytest.mark.parametrize("failure", [OSError("network down"), TimeoutError(),
                                     RuntimeError(f"https://api.telegram.org/bot{TOKEN}/x")])
def test_send_failure_never_raises_and_never_logs_the_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], failure: Exception,
) -> None:
    def transport(url: str, body: bytes, timeout: float) -> int:
        raise failure

    assert notify.send("restore test failed", config_path=_config(tmp_path), transport=transport) is False
    err = capsys.readouterr().err
    assert "not delivered" in err and TOKEN not in err


def test_telegram_refusal_is_reported_as_not_delivered(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert notify.send("x", config_path=_config(tmp_path), transport=lambda u, b, t: 401) is False
    assert "HTTP 401" in capsys.readouterr().err


def test_cli_exits_zero_even_when_nothing_can_be_delivered(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(OPS / "bfx_notify.py"), "--config", str(tmp_path / "absent.env"),
         "--level", "critical", "unit", "failed"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert completed.returncode == 0
    assert "not delivered" in completed.stderr


def test_long_messages_are_truncated_for_telegram() -> None:
    assert len(notify.format_message("x" * 10_000, level="info", host="h")) <= 3900


# --------------------------------------------------------------------------- backup check

NOW = 1_790_000_000.0
NOW_MS = int(NOW * 1000)


def _evidence(**overrides: Any) -> dict[str, Any]:
    return {"schema_version": 1, "kind": "backup", "measured": True, "rpo_seconds": 42,
            "observed_at_ms": NOW_MS - 60_000, **overrides}


@pytest.mark.parametrize(("evidence", "expected"), [
    (_evidence(), []),
    (_evidence(rpo_seconds=301), ["rpo_exceeded:301s"]),
    (_evidence(observed_at_ms=NOW_MS - 20 * 60_000), ["backup_evidence_stale:20min"]),
    ({"measured": False, "error_code": "archive_lag_exceeded", "observed_at_ms": NOW_MS},
     ["backup_unmeasured:archive_lag_exceeded"]),
    ({"measured": False, "error_code": "EVIL; rm -rf", "observed_at_ms": NOW_MS},
     ["backup_unmeasured:unknown"]),
    (_evidence(rpo_seconds=True), ["backup_evidence_invalid"]),
    (_evidence(observed_at_ms=NOW_MS + 3_600_000), ["backup_evidence_from_future"]),
    (None, ["backup_evidence_unavailable"]),
])
def test_backup_problems(evidence: dict[str, Any] | None, expected: list[str]) -> None:
    assert check.backup_problems(evidence, now_ms=NOW_MS, rpo_limit_s=300, max_age_s=900) == expected


class CheckHarness:
    def __init__(self, tmp_path: Path) -> None:
        self.evidence = tmp_path / "backup.json"
        self.heartbeat = tmp_path / "restore-heartbeat.json"
        self.weekly = tmp_path / "weekly-report-heartbeat.json"
        self.state = tmp_path / "state" / "backup-check.json"
        self.notices: list[tuple[str, str]] = []
        self.now = NOW
        self.restore_enabled = False
        self.weekly_enabled = False
        self.disk_percent = 37
        self.clock_sync: str | None = "yes"

    def write(self, **overrides: Any) -> None:
        now_ms = int(self.now * 1000)
        self.evidence.write_text(json.dumps(_evidence(observed_at_ms=now_ms - 60_000, **overrides)))

    def run(self) -> int:
        enabled = {"bfx-restore-test.timer": self.restore_enabled,
                   "bfx-weekly-report.timer": self.weekly_enabled}

        def status(argv: Sequence[str]) -> int:
            assert list(argv[:3]) == ["systemctl", "is-enabled", "--quiet"]
            return 0 if enabled[argv[3]] else 1

        def disk_usage(path: Path) -> tuple[int, int, int]:
            assert path == Path("/")
            return 100, self.disk_percent, 100 - self.disk_percent

        result = check.run_check(
            evidence=self.evidence, heartbeat=self.heartbeat, restore_timer="bfx-restore-test.timer",
            state_path=self.state, rpo_limit_s=300, max_age_s=900, restore_max_age_s=35 * 86_400,
            realert_s=6 * 3600, notify=lambda level, text: self.notices.append((level, text)),
            weekly_heartbeat=self.weekly, weekly_timer="bfx-weekly-report.timer",
            weekly_max_age_s=8 * 86_400, disk_path=Path("/"), disk_warn_percent=85,
            disk_critical_percent=95, status=status, disk_usage=disk_usage,
            clock_sync=lambda: self.clock_sync, clock=lambda: self.now,
        )
        self.now += 300
        return int(result)


def test_rpo_breach_alerts_once_confirmed_then_dedups_then_recovers(tmp_path: Path) -> None:
    h = CheckHarness(tmp_path)
    h.write()
    assert h.run() == 0 and h.notices == []
    h.write(rpo_seconds=900)
    assert h.run() == 1 and h.notices == []           # first sighting: not yet confirmed
    h.write(rpo_seconds=1200)
    assert h.run() == 1
    assert [level for level, _ in h.notices] == ["critical"]
    assert "rpo_exceeded:1200s" in h.notices[0][1]
    h.write(rpo_seconds=1500)
    assert h.run() == 1 and len(h.notices) == 1       # same problem: de-duplicated
    h.now += 6 * 3600
    h.write(rpo_seconds=1800)
    assert h.run() == 1 and len(h.notices) == 2       # re-alert after the window
    h.write()
    assert h.run() == 0
    assert h.notices[-1][0] == "info" and "recovered" in h.notices[-1][1]
    h.write()
    assert h.run() == 0 and len(h.notices) == 3


def test_transient_refresh_marker_does_not_alert(tmp_path: Path) -> None:
    h = CheckHarness(tmp_path)
    h.write(measured=False, error_code="backup_refresh_incomplete")
    assert h.run() == 1
    h.write()
    assert h.run() == 0
    assert h.notices == []


def test_stopped_status_timer_is_detected_by_evidence_age(tmp_path: Path) -> None:
    h = CheckHarness(tmp_path)
    h.write()
    h.now += 30 * 60
    h.run()
    h.run()
    assert h.notices and "backup_evidence_stale" in h.notices[0][1]


def test_restore_heartbeat_is_only_required_while_the_weekly_test_is_enabled(tmp_path: Path) -> None:
    h = CheckHarness(tmp_path)
    for _ in range(3):
        h.write()
        assert h.run() == 0
    assert h.notices == []
    h.restore_enabled = True
    for _ in range(2):
        h.write()
        h.run()
    assert "restore_test_no_heartbeat" in h.notices[0][1]
    h.heartbeat.write_text(json.dumps({"observed_at_ms": int(h.now * 1000)}))
    h.write()
    assert h.run() == 0
    # Monthly cadence: a 34-day-old heartbeat is fine, 36 days is stale.
    for days, expected in ((34, []), (36, ["restore_test_stale:36d"])):
        beat = {"observed_at_ms": int(h.now * 1000) - days * 86_400_000}
        assert check.restore_problems(beat, now_ms=int(h.now * 1000), max_age_s=35 * 86_400,
                                      timer_enabled=True) == expected


def _beat(h: CheckHarness, path: Path, *, days_old: float = 0) -> None:
    path.write_text(json.dumps({"observed_at_ms": int((h.now - days_old * 86_400) * 1000)}))


def _confirm(h: CheckHarness) -> int:
    """Two runs: a problem alerts only once seen twice in a row."""
    h.write()
    h.run()
    h.write()
    return h.run()


def test_disabling_the_restore_timer_on_a_host_that_ran_restores_warns(tmp_path: Path) -> None:
    h = CheckHarness(tmp_path)
    _beat(h, h.heartbeat, days_old=3)
    assert _confirm(h) == 1
    assert h.notices == [("warning", h.notices[0][1])]
    assert "restore_timer_disabled" in h.notices[0][1]


def test_a_new_host_without_a_restore_heartbeat_gets_no_alert(tmp_path: Path) -> None:
    h = CheckHarness(tmp_path)
    assert _confirm(h) == 0 and h.notices == []


def test_a_stale_restore_heartbeat_is_critical_even_with_the_timer_disabled(tmp_path: Path) -> None:
    h = CheckHarness(tmp_path)
    _beat(h, h.heartbeat, days_old=40)
    _confirm(h)
    assert h.notices[0][0] == "critical" and "restore_test_stale:40d" in h.notices[0][1]


@pytest.mark.parametrize(("percent", "expected"), [
    (84, None), (85, ("warning", "disk_usage_high:85%")),
    (95, ("critical", "disk_usage_critical:95%")),
])
def test_disk_usage_thresholds(tmp_path: Path, percent: int,
                               expected: tuple[str, str] | None) -> None:
    h = CheckHarness(tmp_path)
    h.disk_percent = percent
    assert _confirm(h) == (0 if expected is None else 1)
    if expected is None:
        assert h.notices == []
    else:
        assert h.notices[0][0] == expected[0] and expected[1] in h.notices[0][1]


@pytest.mark.parametrize("value, problem", [("no", "clock_not_synchronized"),
                                            (None, "clock_sync_unknown")])
def test_an_unsynchronized_clock_warns_once_confirmed(
    tmp_path: Path, value: str | None, problem: str,
) -> None:
    h = CheckHarness(tmp_path)
    h.write()
    h.clock_sync = value
    assert h.run() == 1 and h.notices == []           # first sighting
    h.write()
    assert h.run() == 1
    assert h.notices == [("warning", h.notices[0][1])] and problem in h.notices[0][1]
    h.clock_sync = "yes"
    h.write()
    assert h.run() == 0 and h.notices[-1][0] == "info"


def test_the_clock_probe_reads_timedatectl(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> Any:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="yes\n", stderr="")

    monkeypatch.setattr(check.subprocess, "run", run)
    assert check._timedatectl_synchronized() == "yes"
    assert seen == [["timedatectl", "show", "-p", "NTPSynchronized", "--value"]]
    monkeypatch.setattr(check.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert check._timedatectl_synchronized() is None


def test_unreadable_disk_usage_is_reported_not_raised() -> None:
    def broken(path: Path) -> tuple[int, int, int]:
        raise OSError("statvfs failed")

    assert check.disk_problems(Path("/"), warn_percent=85, critical_percent=95,
                               usage=broken) == ["disk_usage_unavailable"]


def test_disk_percent_matches_df_use_column() -> None:
    """df's Use% (the vm-facts figure): used / (used + available), not used / total."""
    assert check.disk_problems(Path("/"), warn_percent=85, critical_percent=95,
                               usage=lambda _: (200, 85, 10)) == ["disk_usage_high:89%"]


@pytest.mark.parametrize(("enabled", "days_old", "alerts"), [
    (True, 9, True), (True, 7, False), (False, 30, False),
])
def test_a_stale_weekly_report_warns_only_while_its_timer_is_enabled(
    tmp_path: Path, enabled: bool, days_old: int, alerts: bool,
) -> None:
    h = CheckHarness(tmp_path)
    h.weekly_enabled = enabled
    _beat(h, h.weekly, days_old=days_old)
    _confirm(h)
    if alerts:
        assert h.notices[0][0] == "warning"
        assert f"weekly_report_stale:{days_old}d" in h.notices[0][1]
    else:
        assert h.notices == []


def test_no_weekly_report_heartbeat_yet_is_not_a_problem(tmp_path: Path) -> None:
    h = CheckHarness(tmp_path)
    h.weekly_enabled = True
    assert _confirm(h) == 0 and h.notices == []


@pytest.mark.parametrize("problem", [
    "weekly_report_heartbeat_invalid", "disk_usage_unavailable", "restore_timer_disabled",
])
def test_problems_that_page_as_a_warning(problem: str) -> None:
    state: dict[str, Any] = {"kinds": [problem]}
    message, _ = check.decide(state, [problem], now=NOW, realert_s=6 * 3600)
    assert message is not None and message[0] == "warning"


def test_a_corrupt_restore_heartbeat_is_critical_even_with_the_timer_disabled(
    tmp_path: Path,
) -> None:
    h = CheckHarness(tmp_path)
    h.heartbeat.write_text(json.dumps({"observed_at_ms": "yesterday"}))
    _confirm(h)
    assert h.notices[0][0] == "critical"
    assert "restore_test_heartbeat_invalid" in h.notices[0][1]


def test_a_warning_plus_a_critical_problem_pages_as_critical(tmp_path: Path) -> None:
    h = CheckHarness(tmp_path)
    h.disk_percent = 90
    _beat(h, h.heartbeat, days_old=40)
    _confirm(h)
    assert h.notices[0][0] == "critical"


# --------------------------------------------------------------------------- restore test

class DrillHarness:
    def __init__(self, tmp_path: Path) -> None:
        self.evidence = tmp_path / "restore-ledger.json"
        self.heartbeat = tmp_path / "restore-heartbeat.json"
        self.drill = tmp_path / "restore-drill.sh"
        self.calls: list[list[str]] = []
        self.exit = 0
        self.report: dict[str, Any] | None = {
            "measured": True, "kind": "restore_ledger", "restore_test": True,
            "observed_at_ms": NOW_MS - 5_000,
            "restore_run_id": "20261001T091700Z-abc", "rto_seconds": 212,
            "target_backup_label": "20261001-031700F_20261001-031700D",
            "ledger": {"scopes": [{"exchange_account_id": "a"}], "rows_compared": 4_321},
        }

    def run(self) -> int:
        def runner(argv: Sequence[str], timeout: float) -> int:
            self.calls.append(list(argv))
            if self.report is not None:
                self.evidence.write_text(json.dumps(self.report))
            return self.exit

        return int(restore.run_restore_test(drill=self.drill, evidence=self.evidence,
                                            heartbeat=self.heartbeat, timeout=7000,
                                            runner=runner, clock=lambda: NOW))


def test_restore_test_runs_the_drill_without_a_mode_or_configuration(tmp_path: Path) -> None:
    h = DrillHarness(tmp_path)
    assert h.run() == 0
    # No mode: the drill of the release under test picks the verification.
    assert h.calls == [[str(h.drill), "--restore-test", "--output", str(h.evidence)]]
    beat = json.loads(h.heartbeat.read_text())
    assert (beat["observed_at_ms"], beat["restore_run_id"], beat["rto_seconds"]) == (
        NOW_MS, "20261001T091700Z-abc", 212)
    assert (beat["ledger_scopes"], beat["ledger_rows_compared"]) == (1, 4_321)
    assert "event_seq" not in beat and "legacy_drill" not in beat


def test_failed_drill_keeps_the_old_heartbeat(tmp_path: Path) -> None:
    h = DrillHarness(tmp_path)
    h.heartbeat.write_text('{"observed_at_ms": 1}')
    h.exit = 2
    assert h.run() == 2
    assert json.loads(h.heartbeat.read_text()) == {"observed_at_ms": 1}


@pytest.mark.parametrize("report", [
    None,
    {"measured": False, "kind": "restore_ledger", "restore_test": True, "observed_at_ms": NOW_MS},
    {"measured": True, "kind": "restore_ledger", "restore_test": True,
     "observed_at_ms": NOW_MS - 2 * 3_600_000},
    # A receipt that does not say it is the restore test (a baseline drill's) is no pass.
    {"measured": True, "kind": "restore", "observed_at_ms": NOW_MS},
    {"measured": True, "kind": "restore_ledger", "restore_test": "yes", "observed_at_ms": NOW_MS},
])
def test_success_exit_without_fresh_measured_restore_test_evidence_is_a_failure(
    tmp_path: Path, report: dict[str, Any] | None,
) -> None:
    h = DrillHarness(tmp_path)
    h.report = report
    assert h.run() == 2
    assert not h.heartbeat.exists()


