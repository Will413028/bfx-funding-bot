"""VM probe scripts read /admin/trading-status keys the service actually emits.

`halt-watch.sh` and `l3-verify-24h.sh` embed a Python snippet that indexes the
status payload by key. When `_capital_status` dropped `deployable_headroom`,
both snippets kept indexing it and died on KeyError — halt-watch then logged
only "probe failed", hiding the halt state it exists to report. These tests
run each embedded snippet against payloads produced by the real
TradingStatusService, so a renamed key fails here instead of on the VM.
"""
from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from tests.modules.admin.test_trading_status import _cell, _service

ROOT = Path(__file__).resolve().parents[3]


def _snippet(script: str) -> str:
    text = (ROOT / "deploy/vm" / script).read_text()
    match = re.search(r'python -c "\n(.*?)\n" ', text, re.DOTALL)
    assert match, f"no embedded python -c block in {script}"
    # The only shell expansion inside either block.
    return match.group(1).replace("${N_INTENT:-0}", "0")


async def _payloads(spendable: str | None) -> dict[str, Any]:
    service = _service(cells=[_cell("fUST")])
    status = await service.snapshot()
    if spendable is not None:
        # The available-capital shape; the blocked shape above has no amounts.
        status["symbols"]["fUST"] = {"capital_available": True, "spendable": spendable}
    return {"/admin/trading-status": status,
            "/admin/dry-evaluate": await service.dry_run()}


def _run(script: str, payloads: dict[str, Any]) -> str:
    def urlopen(request: Any, timeout: float) -> io.BytesIO:
        path = request.full_url.removeprefix("http://127.0.0.1:8080")
        return io.BytesIO(json.dumps(payloads[path]).encode())

    out = io.StringIO()
    with mock.patch("urllib.request.urlopen", urlopen), \
            mock.patch.dict("os.environ", {"BFX_ADMIN_TOKEN": "t"}), redirect_stdout(out):
        exec(compile(_snippet(script), script, "exec"), {})
    return out.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(("spendable", "funded"), [(None, "none"), ("0", "none"), ("12.5", "fUST")])
async def test_halt_watch_reports_funded_symbols_from_spendable(
    spendable: str | None, funded: str,
) -> None:
    assert f"(funded={funded})" in _run("halt-watch.sh", await _payloads(spendable))


@pytest.mark.asyncio
async def test_l3_verify_prints_each_probed_symbol() -> None:
    out = _run("l3-verify-24h.sh", await _payloads("12.5"))
    assert "fUST blocked_by=" in out and "spendable= 12.5" in out
