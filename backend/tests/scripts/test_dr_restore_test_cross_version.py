"""The restore-test gate across the S1-8 PR-D boundary (installed tooling vs. target drill).

bfx-deploy runs the TARGET release's drill with the INSTALLED (previous release's) wrapper and
unit, and the verifier is the CURRENTLY DEPLOYED image; the target's tooling is installed only
after a successful deploy. PR-D removes the drill's transitional ``--prefix`` call and the
boot check's stdin fallback, and the wrapper's reverse fallback and ``--legacy-*`` arguments:

* deploying PR-D: d44fc7ab's wrapper (``cross_version/bfx_restore_test_d44fc7ab.py``, a
  byte copy) with d44fc7ab's unit arguments runs THIS drill (its real ``--help`` probe, its real
  CLI, a faked Docker) and accepts the receipt;
* reverting to d44fc7ab after PR-D: THIS wrapper with THIS unit's arguments runs a drill with
  d44fc7ab's argparse surface and accepts its ``--restore-test`` receipt;
* the install windows: the unit now passes only ``--drill`` (the wrapper owns its paths), so
  d44fc7ab's wrapper under this unit and this wrapper under d44fc7ab's unit (its
  ``--evidence``/``--heartbeat`` are the defaults, its ``--legacy-*`` are accepted and ignored)
  both pass.

The image side: the drill now runs ``python -m bfx_funding_bot.apps.restore_boot_check`` in the
deployed image and parses its JSON by required keys and types, ignoring keys a newer image
adds at any level (the last two tests); every image since PR-C has it, and the module is
unchanged since d44fc7ab but for its docstring (``test_ledger_restore_verification`` runs it, as the drill does, on a
restored clone with the drill's grants only).

Delete this file and ``cross_version/`` once PR-D is deployed (a revert past it then reaches a
drill with the same contract on both sides).

Mutations (one at a time): give the verifier back ``-i`` or rename the drill's
``--restore-test``: the first test fails; drop ``--restore-test`` from the wrapper's argv: the
second fails; stop ignoring ``--legacy-config``/``--legacy-evidence`` in the wrapper:
``test_the_previous_unit_runs_this_wrapper`` fails; give the unit ``--evidence`` again:
``test_this_unit_runs_the_previous_wrapper`` fails; compare the boot check's key sets for
equality again: the extra-keys test fails.
"""
from __future__ import annotations

import importlib.util
import json
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.scripts import test_offsite_dr_ledger as dr

ROOT = Path(__file__).resolve().parents[3]
DRILL = ROOT / "deploy/vm/pgbackrest/restore-drill.sh"
UNIT = ROOT / "deploy/vm/systemd/bfx-restore-test@.service"
D44_UNIT_EXEC_START = (
    "/usr/local/lib/bfx-ops/current/ops/.venv/bin/python "
    "/usr/local/lib/bfx-ops/current/ops/bfx_restore_test.py "
    "--drill /home/ubuntu/bfx-releases/%i/deploy/vm/pgbackrest/restore-drill.sh "
    "--evidence /home/ubuntu/bfx/dr-evidence/restore-ledger.json "
    "--heartbeat /home/ubuntu/bfx/dr-evidence/restore-heartbeat.json "
    "--legacy-config /home/ubuntu/bfx/restore-test.json "
    "--legacy-evidence /home/ubuntu/bfx/dr-evidence/restore-prefix.json"
)


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


d44_wrapper = _load("bfx_restore_test_d44fc7ab",
                    Path(__file__).parent / "cross_version/bfx_restore_test_d44fc7ab.py")
this_wrapper = _load("bfx_restore_test_this", ROOT / "deploy/vm/ops/bfx_restore_test.py")


def _unit_argv(exec_start: str, tmp_path: Path, drill: Path) -> list[str]:
    """The wrapper's argv from a unit's ExecStart, with the host paths moved under tmp_path."""
    words = shlex.split(exec_start)[2:]
    argv: list[str] = []
    for flag, value in zip(words[::2], words[1::2], strict=True):
        argv += [flag, str(drill if flag == "--drill" else tmp_path / Path(value).name)]
    return argv


def _this_unit_exec_start() -> str:
    [line] = [line for line in UNIT.read_text().splitlines() if line.startswith("ExecStart=")]
    return line.removeprefix("ExecStart=")


def test_the_deployed_wrapper_and_unit_pass_with_this_drill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deploying PR-D: d44fc7ab's wrapper probes this drill's real --help, then runs its CLI."""
    original = dr.drill_module._config_is_clean_tracked
    monkeypatch.setattr(dr.drill_module, "_config_is_clean_tracked",
                        lambda path: True if path == dr.CONFIG_PATH else original(path))
    fake = dr.FakeDocker()
    real_drill = dr.drill_module.RestoreDrill
    template = dr._ledger_drill(tmp_path, fake)   # the faked Docker, secrets and clocks

    def drill_for(*, output_path: Path) -> Any:
        return real_drill(
            command_runner=fake, stream_runner=fake.stream, config_path=dr.CONFIG_PATH,
            secret_dir=template._secret_dir, output_path=output_path,
            run_id_factory=lambda: dr.RUN_ID, password_factory=lambda: "DATABASE-PASSWORD-SENTINEL",
            postgres_uid=template._postgres_uid, postgres_gid=template._postgres_gid)

    monkeypatch.setattr(dr.drill_module, "RestoreDrill", drill_for)
    calls: list[list[str]] = []

    def runner(argv: Sequence[str], timeout: float) -> int:
        calls.append(list(argv))
        assert argv[0] == str(DRILL)
        return int(dr.drill_module.main(list(argv[1:])))

    run = d44_wrapper.run_restore_test
    monkeypatch.setattr(d44_wrapper, "run_restore_test",
                        lambda **kwargs: run(**kwargs, runner=runner))
    argv = _unit_argv(D44_UNIT_EXEC_START, tmp_path, DRILL)
    assert d44_wrapper.main(argv) == 0

    # Its probe found --restore-test, so it ran the mode-free form, not the --prefix one.
    assert calls == [[str(DRILL), "--restore-test", "--output", str(tmp_path / "restore-ledger.json")]]
    heartbeat = json.loads((tmp_path / "restore-heartbeat.json").read_text())
    assert heartbeat["legacy_drill"] is False
    assert heartbeat["ledger_scopes"] == 1
    assert not (tmp_path / "restore-prefix.json").exists()
    # The verifier is the image's own entry, nothing piped in.
    verifier = fake.find(lambda c: c[:2] == ("docker", "run"))
    assert verifier[-2:] == ("-m", "bfx_funding_bot.apps.restore_boot_check")
    assert "-i" not in verifier and verifier not in fake.inputs


_D44_DRILL_STUB = """#!{python}
# d44fc7ab's restore_drill.py argparse surface (--restore-test, the acceptance drill, and the
# transitional --prefix/--rehearsal); for --restore-test writes a fresh measured receipt.
import argparse, json, os, sys, time
parser = argparse.ArgumentParser()
for flag in ("--account-id", "--environment", "--projector-version", "--backup-label",
             "--target-time", "--output", "--backend-image", "--cells", "--scope",
             "--database-name", "--production-container"):
    parser.add_argument(flag)
for flag in ("--restore-test", "--prefix", "--rehearsal"):
    parser.add_argument(flag, action="store_true")
args = parser.parse_args()
if not args.restore_test or args.output is None or args.prefix:
    sys.exit(2)
os.makedirs(os.path.dirname(args.output), exist_ok=True)
with open(args.output, "w") as handle:
    json.dump({{"measured": True, "kind": "restore_ledger", "restore_test": True,
               "observed_at_ms": int(time.time() * 1000), "ledger": {{"scopes": [{{}}]}}}}, handle)
"""


def test_this_wrapper_and_unit_pass_with_a_reverted_d44fc7ab_drill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reverting to d44fc7ab after PR-D: this wrapper and unit, d44fc7ab's drill surface."""
    drill = tmp_path / "restore-drill.sh"
    drill.write_text(_D44_DRILL_STUB.format(python=sys.executable))
    drill.chmod(0o755)
    monkeypatch.setattr(this_wrapper.Path, "home", staticmethod(lambda: tmp_path))
    argv = _unit_argv(_this_unit_exec_start(), tmp_path, drill)
    assert argv == ["--drill", str(drill)]
    assert this_wrapper.main(argv) == 0
    heartbeat = json.loads((tmp_path / "bfx/dr-evidence/restore-heartbeat.json").read_text())
    assert heartbeat["ledger_scopes"] == 1 and "legacy_drill" not in heartbeat
    assert 0 <= int(time.time() * 1000) - heartbeat["restore_observed_at_ms"] < 600_000


def _d44_surface_drill(tmp_path: Path) -> Path:
    drill = tmp_path / "restore-drill.sh"
    drill.write_text(_D44_DRILL_STUB.format(python=sys.executable))
    drill.chmod(0o755)
    return drill


def test_the_previous_unit_runs_this_wrapper(tmp_path: Path) -> None:
    """Install window one way: ``current`` points at this wrapper, systemd still has d44fc7ab's
    unit loaded (``--evidence``/``--heartbeat`` with the defaults, ``--legacy-*`` ignored)."""
    drill = _d44_surface_drill(tmp_path)
    completed = subprocess.run(
        [sys.executable, str(ROOT / "deploy/vm/ops/bfx_restore_test.py"),
         *_unit_argv(D44_UNIT_EXEC_START, tmp_path, drill)],
        capture_output=True, text=True, check=False, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert json.loads((tmp_path / "restore-heartbeat.json").read_text())["ledger_scopes"] == 1
    assert not (tmp_path / "restore-prefix.json").exists()


def test_this_unit_runs_the_previous_wrapper(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """Install window the other way: this unit (only ``--drill``) with d44fc7ab's wrapper, whose
    default receipt and heartbeat paths are the ones this unit no longer passes."""
    drill = _d44_surface_drill(tmp_path)
    monkeypatch.setattr(d44_wrapper.Path, "home", staticmethod(lambda: tmp_path))
    argv = _unit_argv(_this_unit_exec_start(), tmp_path, drill)
    assert argv == ["--drill", str(drill)]
    assert d44_wrapper.main(argv) == 0
    heartbeat = json.loads((tmp_path / "bfx/dr-evidence/restore-heartbeat.json").read_text())
    assert (heartbeat["ledger_scopes"], heartbeat["legacy_drill"]) == (1, False)
    assert (tmp_path / "bfx/dr-evidence/restore-ledger.json").is_file()


# -- the boot check's output: produced by the DEPLOYED image, parsed by the TARGET drill ------

def _boot_payload() -> dict[str, Any]:
    return json.loads(dr._boot())


def test_a_newer_images_extra_keys_at_every_level_parse_and_stay_out_of_the_receipt() -> None:
    payload = _boot_payload()
    payload["image_note"] = "top"
    payload["boot"]["checks"] = ["schema_head", "realm"]
    [scope] = payload["boot"]["scopes"]
    scope["seed_actor"] = "ledger_seed:switch-x"
    [read] = scope["reads"]
    read["max_snapshot_age_ms"] = 600_000
    boot = dr.ledger.parse_boot(json.dumps(payload) + "\n", dr._bounds())
    assert boot == dr.ledger.parse_boot(dr._boot(), dr._bounds())
    assert set(boot) == {"schema_head", "realm", "authority", "scopes"}
    assert set(boot["scopes"][0]) == {"exchange_account_id", "deployment_environment",
                                      "basis_id", "reads"}
    assert set(boot["scopes"][0]["reads"][0]) == {"symbol", "cell_id", "basis_id", "result"}


@pytest.mark.parametrize(("level", "key", "value"), [
    ("boot", "realm", None), ("boot", "scopes", "x"), ("boot", "authority", ...),
    ("scope", "basis_id", None), ("scope", "reads", ...), ("scope", "exchange_account_id", 1),
    ("read", "result", ...), ("read", "basis_id", 7), ("read", "symbol", None),
])
def test_a_missing_or_mistyped_required_key_still_refuses(level: str, key: str, value: Any) -> None:
    payload = _boot_payload()
    target = {"boot": payload["boot"], "scope": payload["boot"]["scopes"][0],
              "read": payload["boot"]["scopes"][0]["reads"][0]}[level]
    if value is ...:
        del target[key]
    else:
        target[key] = value
    with pytest.raises(dr.ledger.LedgerVerificationError, match="restore_output_invalid"):
        dr.ledger.parse_boot(json.dumps(payload) + "\n", dr._bounds())
