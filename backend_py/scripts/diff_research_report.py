"""Week-over-week research diff over JSON report sidecars (Proposal E, review §7).

Reads reports in chronological order (explicit list, or a glob sorted by filename,
which works because reports are date-prefixed) and writes a markdown with the
pre-registered drift / overtake flags. Research-only; exit code is 0 unless the
inputs cannot be read, so the weekly chain never blocks on it.

  uv run python -m scripts.diff_research_report --kind period-structure \\
      --glob "/reports/*-period-structure-book.json" --out /reports/$(date +%F)-weekly-research.md
  uv run python -m scripts.diff_research_report --kind oos --reports a.json b.json c.json
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from bfx_funding_bot.modules.backtest.research_diff import (
    KINDS,
    ReportSnapshot,
    parse_report,
    render_markdown,
)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--kind", required=True, choices=list(KINDS))
    p.add_argument("--reports", nargs="*", type=Path, default=[], help="chronological JSON paths")
    p.add_argument("--glob", default=None, help="glob of JSON reports; sorted by filename")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--recent", type=int, default=12)
    p.add_argument("--streak", type=int, default=3)
    p.add_argument("--max-history", type=int, default=12, help="keep only the last N reports")
    return p.parse_args(list(argv))


def load_history(kind: str, paths: Sequence[Path]) -> list[ReportSnapshot]:
    history: list[ReportSnapshot] = []
    for path in paths:
        payload = json.loads(path.read_text())
        history.append(parse_report(kind, payload, label=path.name))
    return history


def main(argv: Sequence[str]) -> int:
    args = _parse_args(argv)
    paths = list(args.reports)
    if args.glob:
        paths += [Path(p) for p in sorted(glob.glob(args.glob))]
    if not paths:
        print("no reports matched", file=sys.stderr)
        return 1
    paths = paths[-args.max_history:]
    history = load_history(args.kind, paths)
    md = render_markdown(history, kind=args.kind, recent=args.recent, streak=args.streak)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(md)
        print(f"wrote {args.out}")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
