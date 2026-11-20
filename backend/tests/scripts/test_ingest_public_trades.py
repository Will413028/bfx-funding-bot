"""scripts/ingest_public_trades.py: the trade tape's daily files (plan 2026-11-15 step 2)."""

from __future__ import annotations

import gzip
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from scripts import ingest_public_trades as tape

D1 = date(2026, 11, 1)
D2 = date(2026, 11, 2)
D3 = date(2026, 11, 3)
D4 = date(2026, 11, 4)


def ms(day: date, seconds: int = 0) -> int:
    return tape.Day(day).start_ms + seconds * 1000


def at(day: date, hour: int, minute: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC).timestamp() * 1000)


# 2026-11-04 12:00 UTC: 11-03 has ended (and settled), 11-04 has not.
NOW = at(D4, 12)


def trade(tid: int, day: date, seconds: int = 1, amount: float = -1.0, rate: float = 0.0001,
          period: int = 2) -> list[float | int]:
    return [tid, ms(day, seconds), amount, rate, period]


def daily(*days: date) -> list[list[float | int]]:
    """One trade per day (real tapes have trades every day: step 1 spike)."""
    return [trade(1000 + i, d) for i, d in enumerate(days)]


class FakeVenue:
    """Serves /trades/hist like Bitfinex: rows with start <= mts <= end, sort=1, PAGE_LIMIT rows."""

    def __init__(self, rows: Sequence[Sequence[float | int]],
                 fail: dict[int, Exception] | None = None) -> None:
        self.rows = sorted((list(r) for r in rows), key=lambda r: r[1])
        self.fail = fail or {}  # day start ms -> error raised for that day
        self.calls: list[tuple[str, int, int]] = []

    def __call__(self, symbol: str, start: int, end: int) -> list[list[float | int]]:
        self.calls.append((symbol, start, end))
        assert len(self.calls) <= 100, "paging does not converge"
        day_start = start - start % tape.DAY_MS
        if day_start in self.fail:
            raise self.fail[day_start]
        return [r for r in self.rows if start <= r[1] <= end][: tape.PAGE_LIMIT]


def rows_of(path: Path) -> list[tape.Row]:
    return tape.decode(path.read_bytes())


def names(root: Path, symbol: str = "fUST") -> list[str]:
    return sorted(p.name for p in (root / symbol).iterdir() if not p.name.startswith("."))


def run(root: Path, venue: FakeVenue, symbols: Sequence[str] = ("fUST",), start: date = D1,
        now: int = NOW) -> tape.CollectResult:
    sleeps: list[float] = []
    result = tape.collect(root, symbols, start=start, fetch=venue, sleep=sleeps.append,
                          now_ms=lambda: now, log=lambda _m: None)
    assert all(s == tape.DEFAULT_PACE_S for s in sleeps)
    assert len(sleeps) == max(len(venue.calls) - 1, 0)  # every request after the first waits
    return result


def test_writes_one_sorted_file_per_finished_day_and_keeps_the_sign(tmp_path: Path) -> None:
    venue = FakeVenue([
        trade(11, D1, 5, -100.5, 0.0002, 2),
        trade(10, D1, 5, 50.0, 0.0003, 30),   # same mts, lower id: sorted by (mts, id)
        trade(12, D2, 1, -7.25, 0.0001, 120),
        trade(14, D3),
        trade(13, D4),                         # today: never written
    ])
    assert run(tmp_path, venue).written == {"fUST": 3}
    assert rows_of(tape.day_path(tmp_path, "fUST", D1)) == [
        (10, ms(D1, 5), 50.0, 0.0003, 30), (11, ms(D1, 5), -100.5, 0.0002, 2)]
    assert rows_of(tape.day_path(tmp_path, "fUST", D2))[0][2] == -7.25
    assert not tape.day_path(tmp_path, "fUST", D4).exists()


def test_a_day_is_not_written_until_it_has_settled(tmp_path: Path) -> None:
    run(tmp_path, FakeVenue(daily(D1, D2, D3)), now=at(D4, 0, 30))
    assert names(tmp_path) == ["2026-11-01.csv.gz", "2026-11-02.csv.gz"]
    run(tmp_path, FakeVenue(daily(D1, D2, D3)), now=at(D4, 1, 30))
    assert names(tmp_path)[-1] == "2026-11-03.csv.gz"


def test_the_resume_point_comes_from_the_directory(tmp_path: Path) -> None:
    run(tmp_path, FakeVenue(daily(D1)), now=at(D2, 12))
    venue = FakeVenue(daily(D1, D2, D3))
    assert run(tmp_path, venue).written == {"fUST": 2}
    assert sorted(c[1] for c in venue.calls) == [ms(D2), ms(D3)]  # 11-01 not asked again


def test_a_gap_in_the_middle_is_filled(tmp_path: Path) -> None:
    run(tmp_path, FakeVenue(daily(D1, D2, D3)))
    tape.day_path(tmp_path, "fUST", D2).unlink()
    venue = FakeVenue(daily(D1, D2, D3))
    assert run(tmp_path, venue).written == {"fUST": 1}
    assert [c[1] for c in venue.calls] == [ms(D2)]


def test_newest_days_first_and_symbols_alternate(tmp_path: Path) -> None:
    venue = FakeVenue(daily(D1, D2, D3))
    run(tmp_path, venue, symbols=("fUSD", "fUST"))
    assert [(c[0], c[1]) for c in venue.calls] == [
        ("fUSD", ms(D3)), ("fUST", ms(D3)), ("fUSD", ms(D2)), ("fUST", ms(D2)),
        ("fUSD", ms(D1)), ("fUST", ms(D1))]


def test_a_rerun_never_rewrites_an_existing_day(tmp_path: Path) -> None:
    run(tmp_path, FakeVenue(daily(D1, D2, D3)))
    before = tape.day_path(tmp_path, "fUST", D1).read_bytes()
    venue = FakeVenue([trade(1, D1, amount=-999.0)])
    assert run(tmp_path, venue).written == {"fUST": 0}
    assert venue.calls == []
    assert tape.day_path(tmp_path, "fUST", D1).read_bytes() == before
    with pytest.raises(FileExistsError):
        tape.write_day(tape.day_path(tmp_path, "fUST", D1), [])
    assert tape.day_path(tmp_path, "fUST", D1).read_bytes() == before
    assert not list((tmp_path / "fUST").glob(".*.tmp"))


def test_same_trades_give_the_same_bytes() -> None:
    rows = [(1, 5, -1.5, 0.0001, 2)]
    assert tape.encode(rows) == tape.encode(list(rows))
    assert gzip.decompress(tape.encode(rows)).decode() == "id,mts,amount,rate,period\n1,5,-1.5,0.0001,2\n"


def test_paging_by_mts_drops_ids_repeated_at_the_page_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tape, "PAGE_LIMIT", 4)
    # Ids run backwards in time (they are a cross-symbol sequence), so mts is the cursor.
    rows = [[100 - i, ms(D1, i // 2), -1.0, 0.0001, 2] for i in range(9)]
    venue = FakeVenue(rows)
    run(tmp_path, venue, now=at(D2, 12))
    got = rows_of(tape.day_path(tmp_path, "fUST", D1))
    assert sorted(r[0] for r in got) == sorted(r[0] for r in rows)
    assert len(got) == len(rows)
    assert len(venue.calls) > 2


def test_a_full_page_inside_one_millisecond_stops_instead_of_looping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tape, "PAGE_LIMIT", 3)
    rows = [[i, ms(D1, 1), -1.0, 0.0001, 2] for i in range(5)]
    with pytest.raises(tape.StuckPageError):
        tape.fetch_day("fUST", D1, FakeVenue(rows), lambda _s: None, 0)


@pytest.mark.parametrize("error", [
    tape.EmptyDayError("x"), tape.PayloadError("x"), tape.StuckPageError("x"), TimeoutError("x"),
], ids=["empty", "payload", "stuck", "network"])
def test_a_failing_day_stays_missing_and_the_others_still_land(
    tmp_path: Path, error: Exception,
) -> None:
    venue = FakeVenue(daily(D1, D2, D3), fail={ms(D2): error})
    result = run(tmp_path, venue, symbols=("fUSD", "fUST"))
    assert result.written == {"fUSD": 2, "fUST": 2}
    assert [line.split(":")[0] for line in result.failed] == ["fUSD 2026-11-02", "fUST 2026-11-02"]
    assert tape.missing_days(tmp_path, "fUST", D1, NOW) == [D2]  # retried next run


def test_an_empty_answer_is_never_written_as_a_day(tmp_path: Path) -> None:
    result = run(tmp_path, FakeVenue(daily(D1, D3)))
    assert not tape.day_path(tmp_path, "fUST", D2).exists()
    assert result.failed and "EmptyDayError" in result.failed[0]


@pytest.mark.parametrize("payload", [
    [["error", 10020, "limit: invalid"]], [[1, 2, 3]], [[1, ms(D1), "x", 0.1, 2]], [[True, 1, 1, 1, 1]],
])
def test_a_malformed_trade_is_a_payload_error(payload: list[object]) -> None:
    def venue(*_args: object) -> Any:
        return payload

    with pytest.raises(tape.PayloadError):
        tape.fetch_day("fUST", D1, venue, lambda _s: None, 0)


def test_the_first_429_stops_the_run_and_keeps_finished_days(tmp_path: Path) -> None:
    good = FakeVenue(daily(D1, D2, D3))
    calls = {"n": 0}

    def venue(symbol: str, start: int, end: int) -> list[list[float | int]]:
        calls["n"] += 1
        if calls["n"] == 2:
            raise tape.RateLimitedError("429")
        return good(symbol, start, end)

    with pytest.raises(tape.RateLimitedError):
        tape.collect(tmp_path, ["fUST"], start=D1, fetch=venue, sleep=lambda _s: None,
                     now_ms=lambda: NOW, log=lambda _m: None)
    assert calls["n"] == 2  # no retry after a 429
    assert names(tmp_path) == ["2026-11-03.csv.gz"]


def test_main_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def always(error: Exception) -> Any:
        def venue(*_args: object) -> Any:
            raise error
        return venue

    monkeypatch.setattr("time.sleep", lambda _s: None)
    monkeypatch.setattr(tape, "http_fetch", always(tape.RateLimitedError("x")))
    assert tape.main(["--tape-root", str(tmp_path), "--start", "2026-11-01"]) == 3
    monkeypatch.setattr(tape, "http_fetch", FakeVenue([]))  # every day empty -> failed days
    assert tape.main(["--tape-root", str(tmp_path), "--start", "2026-11-01"]) == 5


def test_a_second_run_on_the_same_tape_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_request(*_args: object) -> Any:
        pytest.fail("a locked tape root was fetched into")

    monkeypatch.setattr(tape, "http_fetch", no_request)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    with tape.tape_lock(tmp_path):
        assert tape.main(["--tape-root", str(tmp_path), "--start", "2026-11-01"]) == 7
        with pytest.raises(tape.TapeLockedError), tape.tape_lock(tmp_path):
            pass


def test_http_fetch_does_not_retry_a_429(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    attempts: list[str] = []

    def urlopen(request: object, timeout: float) -> object:
        attempts.append("x")
        raise urllib.error.HTTPError("u", 429, "Too Many", Message(), None)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr("time.sleep", lambda _s: pytest.fail("slept after a 429"))
    with pytest.raises(tape.RateLimitedError):
        tape.http_fetch("fUST", 0, 1)
    assert attempts == ["x"]


def test_pace_below_twenty_seconds_is_refused(tmp_path: Path) -> None:
    assert tape.main(["--tape-root", str(tmp_path), "--pace", "5"]) == 2


def test_an_interrupted_write_leaves_no_day_file_and_is_cleaned_up(tmp_path: Path) -> None:
    (tmp_path / "fUST").mkdir()
    (tmp_path / "fUST" / ".2026-11-01.csv.gz.tmp").write_bytes(b"partial")
    run(tmp_path, FakeVenue(daily(D1, D2, D3)))
    assert names(tmp_path) == ["2026-11-01.csv.gz", "2026-11-02.csv.gz", "2026-11-03.csv.gz"]
    assert not list((tmp_path / "fUST").glob(".*.tmp"))


def test_verify_passes_on_identical_trades_and_never_writes(tmp_path: Path) -> None:
    trades = daily(D1, D2, D3)
    run(tmp_path, FakeVenue(trades))
    path = tape.day_path(tmp_path, "fUST", D1)
    stat = path.stat()
    assert tape.verify(tmp_path, ["fUST"], [D1, D2], fetch=FakeVenue(trades),
                       sleep=lambda _s: None, log=lambda _m: None) == 2
    assert path.stat().st_mtime_ns == stat.st_mtime_ns


@pytest.mark.parametrize("changed", [
    [trade(1000, D1, rate=0.0002)],                    # same count, other rate
    [trade(1000, D1), trade(2, D1, seconds=2)],        # one more trade
], ids=["content", "count"])
def test_verify_refuses_a_different_day_without_overwriting(
    tmp_path: Path, changed: list[list[float | int]],
) -> None:
    run(tmp_path, FakeVenue(daily(D1, D2, D3)))
    before = tape.day_path(tmp_path, "fUST", D1).read_bytes()
    with pytest.raises(tape.TapeMismatchError):
        tape.verify(tmp_path, ["fUST"], [D1], fetch=FakeVenue(changed),
                    sleep=lambda _s: None, log=lambda _m: None)
    assert tape.day_path(tmp_path, "fUST", D1).read_bytes() == before


def _csv(trades: Sequence[Sequence[float | int]]) -> list[str]:
    return ["id,mts,amount,rate,period\n",
            *(f"{t[0]},{t[1]},{t[2]!r},{t[3]!r},{t[4]}\n" for t in trades)]


def test_import_writes_the_same_files_a_fetch_writes_but_not_the_edge_days(tmp_path: Path) -> None:
    d0 = D1 - timedelta(days=1)
    trades = [trade(9, d0, 80_000), trade(11, D1, 5, -100.5, 0.0002, 2),
              trade(10, D1, 5, 50.0, 0.0003, 30), trade(12, D2, 1, -7.25, 0.0001, 120),
              trade(14, D3, 30)]
    fetched, imported = tmp_path / "fetched", tmp_path / "imported"
    run(fetched, FakeVenue(trades), start=D1)
    assert tape.import_csv(imported, "fUST", _csv(trades), now_ms=NOW, log=lambda _m: None) == 2
    for day in (D1, D2):
        assert tape.day_path(imported, "fUST", day).read_bytes() == \
            tape.day_path(fetched, "fUST", day).read_bytes()
    # The source's first (d0) and last (D3) days may be partial: left for collect.
    assert tape.missing_days(imported, "fUST", d0, NOW) == [d0, D3]


def test_import_keeps_existing_files_and_refuses_bad_sources(tmp_path: Path) -> None:
    run(tmp_path, FakeVenue(daily(D1, D2, D3)), start=D1)
    before = tape.day_path(tmp_path, "fUST", D2).read_bytes()
    lines = _csv([trade(9, D1), trade(8, D2, amount=-5.0), trade(7, D3)])
    assert tape.import_csv(tmp_path, "fUST", lines, now_ms=NOW, log=lambda _m: None) == 0
    assert tape.day_path(tmp_path, "fUST", D2).read_bytes() == before
    unordered = _csv([trade(1, D1), trade(2, D2), trade(3, D1, 2)])
    with pytest.raises(ValueError, match="not ordered"):
        tape.import_csv(tmp_path / "x", "fUST", unordered, now_ms=NOW, log=lambda _m: None)
    truncated = [*_csv([trade(1, D1)]), "\n", f"12345,{ms(D1)}"]
    with pytest.raises(ValueError, match="malformed line 4"):
        tape.import_csv(tmp_path / "y", "fUST", truncated, now_ms=NOW, log=lambda _m: None)
