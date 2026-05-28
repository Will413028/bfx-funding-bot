"""Frozen candle fixtures for the deploy gate + derivation --check.

One gzipped JSONL file per (symbol, period_agg, timeframe) series. Only the
fields the backtest reads are stored (mts, close as a string for exact Decimal
round-trip). A content hash over all files pins the dataset so derive_cells
--check can prove the committed YAML was derived from this exact data.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path

from bfx_funding_bot.modules.candles.schemas import FundingCandle

_SUFFIX = ".jsonl.gz"


def _series_path(directory: Path, symbol: str, period_agg: str, timeframe: str) -> Path:
    # Assumes symbol/period_agg/timeframe contain no "_" (current key space:
    # fUST/fUSD, a30/p2/p30, 1h). load_candles splits the stem on "_" into 3.
    return directory / f"{symbol}_{period_agg}_{timeframe}{_SUFFIX}"


def freeze_candles(
    series: dict[tuple[str, str, str], list[FundingCandle]],
    directory: Path,
) -> None:
    """Write each (symbol, period_agg, timeframe) -> candles to a JSONL.gz file.

    Deterministic: candles sorted by mts, fixed JSON key order, mtime-free gzip
    with an empty stored filename so the archive is byte-identical for identical
    content regardless of run time or OS.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for (symbol, period_agg, timeframe), candles in series.items():
        path = _series_path(directory, symbol, period_agg, timeframe)
        rows = [
            json.dumps({"close": str(c.close), "mts": c.mts}, sort_keys=True)
            for c in sorted(candles, key=lambda c: c.mts)
        ]
        payload = ("\n".join(rows) + "\n").encode("utf-8")
        # BytesIO + mtime=0 + no stored filename -> byte-identical archive
        # for identical content (GzipFile filename= stores in header, not path).
        # compresslevel pinned so a future CPython default change can't shift
        # the bytes. NOTE: the deflate stream is still zlib-version dependent,
        # so the gate relies on freeze-once-locally + commit; CI only READS the
        # committed .jsonl.gz bytes (via fixture_data_hash), never re-freezes.
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, compresslevel=9) as gz:
            gz.write(payload)
        path.write_bytes(buf.getvalue())


def load_candles(path: Path) -> list[FundingCandle]:
    """Load one frozen series; symbol/period_agg/timeframe are parsed from the filename."""
    stem = path.name[: -len(_SUFFIX)] if path.name.endswith(_SUFFIX) else path.stem
    parts = stem.split("_")
    if len(parts) != 3:
        raise ValueError(
            f"Expected 3-part filename <symbol>_<period_agg>_<timeframe>, got: {path.name!r}"
        )
    symbol, period_agg, timeframe = parts
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        out: list[FundingCandle] = []
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            out.append(
                FundingCandle(
                    symbol=symbol,
                    timeframe=timeframe,
                    period_agg=period_agg,
                    mts=int(rec["mts"]),
                    close=rec["close"],
                )
            )
    return out


def fixture_data_hash(directory: Path) -> str:
    """SHA-256 over all *.jsonl.gz raw bytes, ordered by filename. Hex digest."""
    h = hashlib.sha256()
    for path in sorted(directory.glob(f"*{_SUFFIX}")):
        h.update(path.name.encode("utf-8"))
        h.update(path.read_bytes())
    return h.hexdigest()
