"""Collect the public Bitfinex funding trade tape as one gzip CSV per UTC day.

Research input only (plan 2026-11-15-trade-tape-research-method, step 2; ADR
2026-11-20): the live bot never reads these files. Layout:

    <tape-root>/<symbol>/<YYYY-MM-DD>.csv.gz     header: id,mts,amount,rate,period

`amount` keeps its sign (negative = the borrower took a resting offer). Rows are
sorted by (mts, id) and the gzip header carries no name or mtime, so the same
trades always give the same bytes.

Convergent: the days to fetch are derived from the directory (every day from
--start through the last finished UTC day that has no file). A day file is
written only after the day has ended (plus SETTLE_MS) and only once complete
(temporary file + rename), so an interrupted run leaves no partial day and the
next run resumes. Existing files are never rewritten; `--verify` re-fetches
existing days and fails (exit 4) when the trades differ, without overwriting.

Rate limits (the VM shares its egress IP with the live bot, and Bitfinex blocks
the whole IP for 60 s on a breach): one request every --pace seconds (default
20) and the first HTTP 429 stops the run (exit 3); finished days stay on disk.

Usage (in the image, working dir /app):
    python -m scripts.ingest_public_trades --tape-root /tape
    python -m scripts.ingest_public_trades --tape-root /tape --verify 2026-11-01:2026-11-03
    python -m scripts.ingest_public_trades --tape-root /tape --import-csv fUSD.csv --symbol fUSD
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

BASE_URL = "https://api-pub.bitfinex.com/v2/trades/{symbol}/hist"
USER_AGENT = "Mozilla/5.0 (compatible; research-client)"
PAGE_LIMIT = 10_000
DEFAULT_PACE_S = 20.0
DEFAULT_START = date(2022, 1, 1)
DEFAULT_SYMBOLS = ("fUSD", "fUST")
HEADER = ("id", "mts", "amount", "rate", "period")
DAY_MS = 86_400_000
# A day is fetched only this long after it ended, so late-published trades are in.
SETTLE_MS = 3_600_000
TRANSIENT_RETRIES = 3

Row = tuple[int, int, float, float, int]
Fetch = Callable[[str, int, int], list[list[float | int]]]


class RateLimitedError(Exception):
    """HTTP 429: stop at once (the block covers the bot's IP too)."""


class TapeMismatchError(Exception):
    """A re-fetched day differs from the file on disk."""


class StuckPageError(Exception):
    """A full page inside one millisecond: MTS paging cannot move past it."""


@dataclass(frozen=True, slots=True)
class Day:
    value: date

    @property
    def start_ms(self) -> int:
        return int(datetime(self.value.year, self.value.month, self.value.day,
                            tzinfo=UTC).timestamp() * 1000)

    @property
    def end_ms(self) -> int:  # inclusive, as the API's `end`
        return self.start_ms + DAY_MS - 1


def day_path(root: Path, symbol: str, day: date) -> Path:
    return root / symbol / f"{day.isoformat()}.csv.gz"


def last_finished_day(now_ms: int) -> date:
    """The newest UTC day that ended at least SETTLE_MS ago."""
    return (datetime.fromtimestamp((now_ms - SETTLE_MS) / 1000, UTC) - timedelta(days=1)).date()


def missing_days(root: Path, symbol: str, start: date, now_ms: int) -> list[date]:
    last = last_finished_day(now_ms)
    present = {p.name.removesuffix(".csv.gz") for p in (root / symbol).glob("*.csv.gz")}
    days, day = [], start
    while day <= last:
        if day.isoformat() not in present:
            days.append(day)
        day += timedelta(days=1)
    return days


def _row(raw: Sequence[float | int]) -> Row:
    return int(raw[0]), int(raw[1]), float(raw[2]), float(raw[3]), int(raw[4])


def fetch_day(symbol: str, day: date, fetch: Fetch, sleep: Callable[[float], None],
              pace_s: float) -> list[Row]:
    """Every trade of one UTC day, paged by MTS; ids repeated at a page edge are dropped."""
    bounds = Day(day)
    cursor, end = bounds.start_ms, bounds.end_ms
    seen_at_cursor: set[int] = set()
    rows: dict[int, Row] = {}
    first = True
    while True:
        if not first:
            sleep(pace_s)
        first = False
        page = [_row(r) for r in fetch(symbol, cursor, end)]
        fresh = [r for r in page if r[0] not in seen_at_cursor and bounds.start_ms <= r[1] <= end]
        for row in fresh:
            rows[row[0]] = row
        if len(page) < PAGE_LIMIT:
            break
        last_mts = page[-1][1]
        if last_mts == cursor and not fresh:
            raise StuckPageError(f"{symbol} {day}: {PAGE_LIMIT} trades at mts={cursor}")
        at_last = {r[0] for r in page if r[1] == last_mts}
        seen_at_cursor = at_last | (seen_at_cursor if last_mts == cursor else set())
        cursor = last_mts
    return sorted(rows.values(), key=lambda r: (r[1], r[0]))


def encode(rows: Iterable[Row]) -> bytes:
    text = io.StringIO()
    writer = csv.writer(text, lineterminator="\n")
    writer.writerow(HEADER)
    for tid, mts, amount, rate, period in rows:
        writer.writerow((tid, mts, repr(amount), repr(rate), period))
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as stream:
        stream.write(text.getvalue().encode())
    return buffer.getvalue()


def decode(blob: bytes) -> list[Row]:
    lines = gzip.decompress(blob).decode().splitlines()
    if not lines or tuple(lines[0].split(",")) != HEADER:
        raise ValueError("not a tape day file")
    return [_row([float(f) if i in (2, 3) else int(f) for i, f in enumerate(line.split(","))])
            for line in lines[1:]]


def write_day(path: Path, rows: list[Row]) -> None:
    """Atomic and write-once: an existing file is never replaced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(path)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(encode(rows))
    os.replace(temporary, path)


def clear_temporaries(root: Path, symbol: str) -> None:
    for stale in (root / symbol).glob(".*.tmp"):
        stale.unlink()


def collect(root: Path, symbols: Sequence[str], *, start: date, fetch: Fetch,
            sleep: Callable[[float], None] = time.sleep, pace_s: float = DEFAULT_PACE_S,
            now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
            log: Callable[[str], None] = print) -> dict[str, int]:
    """Fetch every missing finished day. Returns days written per symbol."""
    written: dict[str, int] = {}
    requested = False
    for symbol in symbols:
        clear_temporaries(root, symbol)
        written[symbol] = 0
        for day in missing_days(root, symbol, start, now_ms()):
            if requested:
                sleep(pace_s)
            requested = True
            rows = fetch_day(symbol, day, fetch, sleep, pace_s)
            write_day(day_path(root, symbol, day), rows)
            written[symbol] += 1
            log(f"{symbol} {day} rows={len(rows)}")
    return written


def verify(root: Path, symbols: Sequence[str], days: Sequence[date], *, fetch: Fetch,
           sleep: Callable[[float], None] = time.sleep, pace_s: float = DEFAULT_PACE_S,
           log: Callable[[str], None] = print) -> int:
    """Re-fetch existing days; raise TapeMismatchError on any difference. Never writes."""
    checked = 0
    for symbol in symbols:
        for day in days:
            path = day_path(root, symbol, day)
            if not path.exists():
                continue
            if checked:
                sleep(pace_s)
            stored = path.read_bytes()
            fresh = encode(fetch_day(symbol, day, fetch, sleep, pace_s))
            if gzip.decompress(fresh) != gzip.decompress(stored):
                raise TapeMismatchError(f"{symbol} {day}: stored {len(decode(stored))} rows, "
                                   f"API {len(decode(fresh))} rows")
            checked += 1
            log(f"{symbol} {day} matches")
    return checked


def _csv_rows(lines: Iterable[str]) -> Iterator[Row]:
    for line in lines:
        f = line.rstrip("\n").split(",")
        if f[0] == "id":
            continue
        yield int(f[0]), int(f[1]), float(f[2]), float(f[3]), int(f[4])


def import_csv(root: Path, symbol: str, lines: Iterable[str], *, now_ms: int,
               log: Callable[[str], None] = print) -> int:
    """Split one MTS-ordered tape CSV (spike output) into day files.

    Streams: holds one day at a time. Skips days not yet finished and days that
    already have a file. The source must be ordered by MTS (the spike fetched with
    sort=1); a day seen twice is an error, not a merge.
    """
    clear_temporaries(root, symbol)
    last = last_finished_day(now_ms)
    written = 0
    current: date | None = None
    done: set[date] = set()
    rows: dict[int, Row] = {}

    def flush() -> None:
        nonlocal written
        if current is None or current > last:
            return
        path = day_path(root, symbol, current)
        if path.exists():
            return
        write_day(path, sorted(rows.values(), key=lambda r: (r[1], r[0])))
        written += 1
        log(f"{symbol} {current} rows={len(rows)} (imported)")

    for row in _csv_rows(lines):
        day = datetime.fromtimestamp(row[1] / 1000, UTC).date()
        if day != current:
            flush()
            if day in done:
                raise ValueError(f"{symbol}: source not ordered by mts ({day} seen twice)")
            if current is not None:
                done.add(current)
            current, rows = day, {}
        rows[row[0]] = row
    flush()
    return written


def http_fetch(symbol: str, start_ms: int, end_ms: int) -> list[list[float | int]]:
    url = (f"{BASE_URL.format(symbol=symbol)}?start={start_ms}&end={end_ms}"
           f"&limit={PAGE_LIMIT}&sort=1")
    for attempt in range(TRANSIENT_RETRIES + 1):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.load(response)
            if not isinstance(payload, list):
                raise ValueError(f"unexpected payload for {symbol}")
            return payload
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise RateLimitedError(url) from None
            if exc.code < 500 or attempt == TRANSIENT_RETRIES:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == TRANSIENT_RETRIES:
                raise
        time.sleep(DEFAULT_PACE_S)
    raise AssertionError("unreachable")


def _day_range(text: str) -> list[date]:
    first, _, last = text.partition(":")
    start, end = date.fromisoformat(first), date.fromisoformat(last or first)
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tape-root", type=Path, required=True)
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    p.add_argument("--pace", type=float, default=DEFAULT_PACE_S)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--verify", metavar="DAY[:DAY]", help="re-fetch existing days and compare")
    mode.add_argument("--import-csv", type=Path, help="split a spike CSV into day files")
    p.add_argument("--symbol", help="the symbol of --import-csv")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.pace < DEFAULT_PACE_S:
        print(f"--pace below {DEFAULT_PACE_S}s is not allowed on the shared IP", file=sys.stderr)
        return 2
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    try:
        if args.import_csv is not None:
            if not args.symbol:
                print("--import-csv needs --symbol", file=sys.stderr)
                return 2
            with args.import_csv.open(encoding="utf-8") as stream:
                n = import_csv(args.tape_root, args.symbol, stream, now_ms=int(time.time() * 1000))
            print(f"imported {n} days")
        elif args.verify is not None:
            n = verify(args.tape_root, symbols, _day_range(args.verify), fetch=http_fetch,
                       pace_s=args.pace)
            print(f"verified {n} day files")
        else:
            written = collect(args.tape_root, symbols, start=args.start, fetch=http_fetch,
                              pace_s=args.pace)
            print(f"written {written}")
    except RateLimitedError:
        print("HTTP 429: stopped (finished days are kept; the next run resumes)", file=sys.stderr)
        return 3
    except TapeMismatchError as exc:
        print(f"tape mismatch, nothing overwritten: {exc}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
