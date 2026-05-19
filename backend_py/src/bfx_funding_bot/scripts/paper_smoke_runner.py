"""Paper smoke runner — outcome-based exit.

Starts daemon subprocess in paper mode, polls Axiom for signal event
count, exits 0 when >= N (N = active cells count) or exits 1/2 on
timeout per Phase 4.2.0 spec D6.

Usage:
    uv run python -m bfx_funding_bot.scripts.paper_smoke_runner [--timeout-min 90]

Exit codes:
    0 OK       -- count >= N before timeout
    1 ERROR    -- timeout with 0 signal events (daemon broken)
    2 PARTIAL  -- timeout with 1 <= count < N (cross-boundary but some cells silent)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal as signal_mod
import subprocess
import sys
from datetime import UTC, datetime

import httpx

log = logging.getLogger("paper_smoke_runner")

DEFAULT_TIMEOUT_MIN = 90
WARMUP_S = 60.0
POLL_INTERVAL_S = 30.0


def _decide_exit_code(
    *,
    count: int,
    n_required: int,
    elapsed_s: float,
    timeout_s: float = DEFAULT_TIMEOUT_MIN * 60,
) -> int:
    """Pure decision: -1 = continue polling, 0/1/2 = exit codes per spec D6."""
    if count >= n_required:
        return 0
    if elapsed_s >= timeout_s:
        if count >= 1:
            return 2
        return 1
    return -1


async def _query_axiom_signal_count(
    *,
    axiom_api_key: str,
    axiom_dataset: str,
    since: datetime,
) -> int:
    """Query Axiom for signal events emitted since `since` timestamp."""
    apl = (
        f"['{axiom_dataset}'] "
        f"| where _time > datetime({since.isoformat()}) "
        f"| where event_type == 'signal' "
        f"| where phase == 'paper' "
        f"| count"
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://api.axiom.co/v1/datasets/_apl?format=tabular",
            headers={"Authorization": f"Bearer {axiom_api_key}"},
            json={"apl": apl},
        )
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("tables", [{}])[0].get("rows", [])
        if not rows:
            return 0
        return int(rows[0][0])


def _count_active_cells() -> int:
    """Read cells.yaml count via load_config (respects BFX_CELLS filter)."""
    from bfx_funding_bot.modules.marketfeed.config import load_config

    return len(load_config().cells)


async def run_smoke(timeout_min: int = DEFAULT_TIMEOUT_MIN) -> int:
    axiom_api_key = os.environ["AXIOM_API_KEY"]
    axiom_dataset = os.environ["AXIOM_DATASET"]
    n_required = _count_active_cells()
    log.info("smoke_start n_required=%d timeout_min=%d", n_required, timeout_min)

    start = datetime.now(UTC)
    daemon_proc = subprocess.Popen(
        ["uv", "run", "bfx-shadow"],
        env={**os.environ, "BFX_PHASE": "paper"},
    )

    try:
        await asyncio.sleep(WARMUP_S)
        while True:
            count = await _query_axiom_signal_count(
                axiom_api_key=axiom_api_key,
                axiom_dataset=axiom_dataset,
                since=start,
            )
            elapsed = (datetime.now(UTC) - start).total_seconds()
            rc = _decide_exit_code(
                count=count,
                n_required=n_required,
                elapsed_s=elapsed,
                timeout_s=timeout_min * 60,
            )
            log.info("poll count=%d elapsed_s=%.0f rc=%d", count, elapsed, rc)
            if rc != -1:
                return rc
            await asyncio.sleep(POLL_INTERVAL_S)
    finally:
        log.info("sending SIGTERM to daemon")
        daemon_proc.send_signal(signal_mod.SIGTERM)
        try:
            daemon_proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            log.error("daemon did not exit within 60s, force kill")
            daemon_proc.kill()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-min", type=int, default=DEFAULT_TIMEOUT_MIN)
    args = parser.parse_args()
    rc = asyncio.run(run_smoke(timeout_min=args.timeout_min))
    log.info("smoke_done exit=%d", rc)
    sys.exit(rc)


if __name__ == "__main__":
    main()
