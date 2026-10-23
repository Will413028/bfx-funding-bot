"""Delivery metrics from the deployments ledger: change failure rate and recovery time.

Read-only over `deployments` (written by bfx-deploy). Definitions follow DORA:

- a change is an attempt that reached production: `deployed`, `rolled_back`, or `failed`
  with `service_degraded` (the bot or the web API left stopped or unhealthy). A `failed`
  attempt that left the running release untouched is not a change;
- a failure is `rolled_back` or a degraded `failed`;
- failed deployment recovery time: for a degraded `failed`, from its end until the next
  `deployed` row ends; for `rolled_back`, the attempt's own duration (an upper bound: the
  ledger does not record when the release turned unhealthy).

Rows written before bfx-deploy recorded `service_degraded` (NULL) are counted as
unclassified, never guessed from `detail`. A migration deploy's planned stop is not a failure.

Run from backend/ (env: DATABASE_URL):
  uv run python -m scripts.report_delivery_metrics [--days 28] [--out FILE]
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.deployments.tables import DeploymentRow


@dataclass(frozen=True, slots=True)
class LedgerRow:
    id: int
    attempt_id: str
    outcome: str
    started_at: datetime
    finished_at: datetime | None
    service_degraded: bool | None


@dataclass(frozen=True, slots=True)
class Failure:
    row: LedgerRow
    recovery: timedelta | None  # None: not recovered yet
    upper_bound: bool


@dataclass(frozen=True, slots=True)
class Metrics:
    start: datetime
    end: datetime
    changes: int
    failures: list[Failure]
    untouched: int
    unclassified: int
    interrupted: int

    @property
    def change_failure_rate(self) -> float | None:
        return len(self.failures) / self.changes if self.changes else None


def _is_failure(row: LedgerRow) -> bool:
    return row.outcome == "rolled_back" or (row.outcome == "failed" and row.service_degraded is True)


def delivery_metrics(rows: Sequence[LedgerRow], *, start: datetime, end: datetime) -> Metrics:
    ordered = sorted(rows, key=lambda r: r.id)
    terminal = [r for r in ordered if r.outcome != "started" and r.finished_at is not None
                and start <= r.finished_at < end]
    classified = [r for r in terminal if r.service_degraded is not None]
    changes = [r for r in classified
               if r.outcome in ("deployed", "rolled_back") or r.service_degraded is True]
    failures = []
    for row in changes:
        if not _is_failure(row):
            continue
        assert row.finished_at is not None
        if row.outcome == "rolled_back":
            failures.append(Failure(row, row.finished_at - row.started_at, upper_bound=True))
            continue
        restored = next((r for r in ordered if r.id > row.id and r.outcome == "deployed"
                         and r.finished_at is not None), None)
        recovery = restored.finished_at - row.finished_at if restored and restored.finished_at else None
        failures.append(Failure(row, recovery, upper_bound=False))
    # A started row whose attempt has no terminal row: bfx-deploy itself died mid-attempt.
    finished = {r.attempt_id for r in ordered if r.outcome != "started"}
    interrupted = sum(1 for r in ordered if r.outcome == "started"
                      and start <= r.started_at < end and r.attempt_id not in finished)
    return Metrics(
        start=start, end=end, changes=len(changes), failures=failures,
        untouched=sum(1 for r in classified if r.outcome == "failed" and r.service_degraded is False),
        unclassified=len(terminal) - len(classified), interrupted=interrupted,
    )


def _duration(value: timedelta) -> str:
    minutes = value.total_seconds() / 60
    return f"{minutes:.0f} min" if minutes < 120 else f"{minutes / 60:.1f} h"


def _median(values: list[timedelta]) -> str:
    return _duration(timedelta(seconds=statistics.median(v.total_seconds() for v in values)))


def render(metrics: Metrics) -> str:
    rate = metrics.change_failure_rate
    recovered = [f.recovery for f in metrics.failures if f.recovery is not None]
    lines = [
        f"Window: {metrics.start:%Y-%m-%d %H:%M} → {metrics.end:%Y-%m-%d %H:%M} UTC",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| changes (reached production) | {metrics.changes} |",
        f"| failures (rolled back or left degraded) | {len(metrics.failures)} |",
        f"| change failure rate | {'—' if rate is None else f'{rate:.1%}'} |",
        f"| median recovery time | {_median(recovered) if recovered else '—'} |",
        f"| longest recovery time | {_duration(max(recovered)) if recovered else '—'} |",
        f"| not yet recovered | {sum(1 for f in metrics.failures if f.recovery is None)} |",
        f"| failed before touching production | {metrics.untouched} |",
        f"| unclassified (written before service_degraded) | {metrics.unclassified} |",
        f"| interrupted (started, never finished) | {metrics.interrupted} |",
    ]
    if metrics.failures:
        lines += ["", "| ledger id | outcome | finished (UTC) | recovery |", "|---:|---|---|---|"]
        for f in metrics.failures:
            assert f.row.finished_at is not None
            recovery = ("not yet" if f.recovery is None
                        else ("≤ " if f.upper_bound else "") + _duration(f.recovery))
            lines.append(f"| {f.row.id} | {f.row.outcome} | {f.row.finished_at:%Y-%m-%d %H:%M} | {recovery} |")
    return "\n".join(lines) + "\n"


async def _amain(args: argparse.Namespace) -> int:
    engine = make_engine(Settings())
    try:
        async with make_session_factory(engine)() as session:
            found = (await session.scalars(select(DeploymentRow))).all()
    finally:
        await engine.dispose()
    rows = [LedgerRow(r.id, str(r.attempt_id), r.outcome, r.started_at, r.finished_at,
                      r.service_degraded) for r in found]
    end = datetime.now(UTC)
    report = render(delivery_metrics(rows, start=end - timedelta(days=args.days), end=end))
    if args.out:
        Path(args.out).write_text(f"# Delivery metrics (deployments ledger)\n\n{report}")
    print(report, end="")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--out")
    sys.exit(asyncio.run(_amain(parser.parse_args())))
