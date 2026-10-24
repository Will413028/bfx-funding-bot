import importlib.util
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/image_os_updates.py"
spec = importlib.util.spec_from_file_location("image_os_updates", SCRIPT)
assert spec is not None and spec.loader is not None
image_os_updates = importlib.util.module_from_spec(spec)
sys.modules["image_os_updates"] = image_os_updates
spec.loader.exec_module(image_os_updates)

# Real `apt list --upgradable` output from the pinned python:3.13-slim base (2026-10-24).
APT = """Listing...
ca-certificates/stable-security 20250419+deb13u1 all [upgradable from: 20250419]
liblzma5/stable-security 5.8.1-1+deb13u2 arm64 [upgradable from: 5.8.1-1+deb13u1]
tzdata/stable-updates 2026e-0+deb13u1 all [upgradable from: 2026c-0+deb13u1]
"""
NOW = datetime(2026, 10, 24, tzinfo=UTC)


def baseline(*entries: tuple[str, str, str]) -> dict[str, list[dict[str, object]]]:
    return {
        "Pending": [
            {"package": p, "version": v, "ignoreUntil": datetime.fromisoformat(u), "reason": "r"}
            for p, v, u in entries
        ]
    }


def test_only_security_suites_count() -> None:
    upgrades = image_os_updates.security_upgrades(APT)
    assert [(u.package, u.version, u.installed) for u in upgrades] == [
        ("ca-certificates", "20250419+deb13u1", "20250419"),
        ("liblzma5", "5.8.1-1+deb13u2", "5.8.1-1+deb13u1"),
    ]


def test_a_suite_list_with_security_counts() -> None:
    line = (
        "openssl/stable,stable-security 3.5.8-1~deb13u1 arm64 [upgradable from: 3.5.7-1~deb13u3]\n"
    )
    assert [u.package for u in image_os_updates.security_upgrades(line)] == ["openssl"]


def test_an_unlisted_security_update_fails() -> None:
    upgrades = image_os_updates.security_upgrades(APT)
    found = image_os_updates.problems(
        upgrades, baseline(("liblzma5", "5.8.1-1+deb13u2", "2026-11-03T00:00:00+00:00")), NOW
    )
    assert found == [
        "ca-certificates: security update 20250419+deb13u1 not installed (image has 20250419)"
    ]


def test_a_listed_update_passes_until_its_date() -> None:
    upgrades = image_os_updates.security_upgrades(APT)
    entries = (
        ("ca-certificates", "20250419+deb13u1", "2026-11-03T00:00:00+00:00"),
        ("liblzma5", "5.8.1-1+deb13u2", "2026-11-03T00:00:00+00:00"),
    )
    assert image_os_updates.problems(upgrades, baseline(*entries), NOW) == []
    later = datetime(2026, 11, 3, tzinfo=UTC)
    assert len(image_os_updates.problems(upgrades, baseline(*entries), later)) == 4


def test_a_newer_version_than_the_listed_one_fails() -> None:
    upgrades = image_os_updates.security_upgrades(APT)
    entries = (
        ("ca-certificates", "20250419+deb13u1", "2026-11-03T00:00:00+00:00"),
        ("liblzma5", "5.8.1-1+deb13u1", "2026-11-03T00:00:00+00:00"),
    )
    assert image_os_updates.problems(upgrades, baseline(*entries), NOW) == [
        "liblzma5: security update 5.8.1-1+deb13u2 not installed (image has 5.8.1-1+deb13u1)"
    ]


def test_an_expired_entry_fails_even_when_installed() -> None:
    found = image_os_updates.problems(
        [], baseline(("liblzma5", "5.8.1-1+deb13u2", "2026-10-01T00:00:00+00:00")), NOW
    )
    assert found == ["liblzma5 5.8.1-1+deb13u2: baseline entry expired 2026-10-01 (r)"]


def test_the_cli_exits_by_the_result(tmp_path: Path) -> None:
    empty = tmp_path / "empty.toml"
    empty.write_text("")
    run = [sys.executable, str(SCRIPT), "--baseline", str(empty)]
    failed = subprocess.run(run, input=APT, capture_output=True, text=True, check=False)
    assert failed.returncode == 1
    assert "::error::liblzma5: security update" in failed.stdout
    clean = subprocess.run(run, input="Listing...\n", capture_output=True, text=True, check=False)
    assert clean.returncode == 0


def test_the_tracked_baseline_parses_and_names_its_reasons() -> None:
    entries = tomllib.loads((ROOT / "image-os-updates.toml").read_text())["Pending"]
    for entry in entries:
        assert {"package", "version", "ignoreUntil", "reason"} <= entry.keys()
        assert entry["ignoreUntil"].tzinfo is not None
