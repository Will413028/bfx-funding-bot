"""The restore-test boot check runs against the image it is piped into, older or newer.

bfx-deploy's restore-test gate feeds the TARGET release's ``ledger_boot_check.py`` to ``python -``
inside the CURRENTLY DEPLOYED image. An image with its own entry
(``bfx_funding_bot.apps.restore_boot_check``) is judged by it; the release before S1-8 PR-C has
none, and the script's fallback (``require_ledger_seed(..., authority=...)``) judges it. Both
are run here for real: that release's ``backend/`` from git (``PRE_LEDGER_ONLY``) on a database
at its own migration head, and this tree's ``src`` on a database at this head; the entry is
also run on its own, as later drills will (``python -m``).

Mutations (each applied alone to deploy/vm/pgbackrest/ledger_boot_check.py and reverted):

* never use the image entry (``image_entry`` returns None): the ``current`` case fails (the
  fallback's ``authority=`` meets the ledger-only guard: boot_check_failed TypeError);
* always use it: the ``pre_ledger_only`` case fails (no such module in that image).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

from bfx_funding_bot.core.schema_head import build_head
from tests.pg_templates import alembic, stamp_realm

from .test_ledger_restore_verification import (
    _A,
    _B,
    PGBACKREST,
    ROOT,
    _template,
    _verifier_url,
    restore_point,
)

pytestmark = pytest.mark.integration

# origin/main before S1-8 PR-C: the API of the image deployed when PR-C rolls out.
PRE_LEDGER_ONLY = "3af116a2"
# That release's migration head: its own schema check refuses any other.
PRE_LEDGER_ONLY_HEAD = "a0b1c2d3e4f5"


@pytest.fixture(scope="session")
def pre_ledger_only_backend(tmp_path_factory) -> Path:
    """``backend/`` of ``PRE_LEDGER_ONLY``, extracted from git (skipped where history is absent)."""
    found = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{PRE_LEDGER_ONLY}^{{commit}}"],
                           capture_output=True, check=False)
    if found.returncode != 0:
        pytest.skip(f"commit {PRE_LEDGER_ONLY} is not in this checkout's history")
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", PRE_LEDGER_ONLY, "backend"],
                             capture_output=True, check=True).stdout
    target = tmp_path_factory.mktemp("pre_ledger_only")
    with tarfile.open(fileobj=BytesIO(archive)) as tar:
        tar.extractall(target, filter="data")
    return target / "backend"


def _pre_ledger_only_template(url: str) -> None:
    alembic(url, "upgrade", PRE_LEDGER_ONLY_HEAD)
    stamp_realm(url, "ci")
    restore_point(url)


def _run(backend: Path, database_url: str) -> subprocess.CompletedProcess[str]:
    """As the drill runs it (the script on stdin, cwd the app root), on ``backend``'s src."""
    env = {**os.environ, "DATABASE_URL": database_url, "PYTHONPATH": str(backend / "src")}
    return subprocess.run(
        [sys.executable, "-"], input=(PGBACKREST / "ledger_boot_check.py").read_text(),
        cwd=backend, env=env, capture_output=True, text=True, check=False, timeout=300,
    )


def _imported_from(backend: Path) -> str:
    probe = "import bfx_funding_bot.apps.authority_support as m; print(m.__file__)"
    return subprocess.run(
        [sys.executable, "-c", probe], cwd=backend, capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONPATH": str(backend / "src")},
    ).stdout.strip()


@pytest.mark.parametrize("image", ["pre_ledger_only", "current"])
def test_the_boot_check_passes_on_both_apis(image, request, pg_templates, pg_clone) -> None:
    if image == "pre_ledger_only":
        backend = request.getfixturevalue("pre_ledger_only_backend")
        url = pg_clone(pg_templates.template(
            f"boot_check_at_{PRE_LEDGER_ONLY_HEAD}", _pre_ledger_only_template))
    else:
        backend = ROOT / "backend"
        url = pg_clone(pg_templates.template("ledger_restore_verification", _template))
    # The run really uses that tree's package, not the installed one.
    assert _imported_from(backend).startswith(str(backend / "src"))
    completed = _run(backend, _verifier_url(url))
    assert completed.returncode == 0, completed.stdout + completed.stderr
    boot = json.loads(completed.stdout.splitlines()[-1])["boot"]
    assert (boot["authority"], boot["realm"]) == ("ledger", "ci")
    [scope] = boot["scopes"]
    assert (scope["exchange_account_id"], scope["basis_id"]) == (_A, _B)
    assert boot["schema_head"] == (PRE_LEDGER_ONLY_HEAD if image == "pre_ledger_only"
                                   else build_head())


def test_the_image_entry_runs_on_its_own(pg_templates, pg_clone) -> None:
    """What a drill after the fallback's removal runs: the image's module, no piped script."""
    backend = ROOT / "backend"
    url = pg_clone(pg_templates.template("ledger_restore_verification", _template))
    completed = subprocess.run(
        [sys.executable, "-m", "bfx_funding_bot.apps.restore_boot_check"], cwd=backend,
        env={**os.environ, "DATABASE_URL": _verifier_url(url)}, capture_output=True, text=True,
        check=False, timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    boot = json.loads(completed.stdout.splitlines()[-1])["boot"]
    assert (boot["authority"], boot["schema_head"]) == ("ledger", build_head())
