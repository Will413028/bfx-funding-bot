"""scripts/ingest_public_trades.py: the trade tape's daily files (plan 2026-11-15 step 2)."""

from __future__ import annotations

import gzip
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from scripts import ingest_public_trades as tape

D1 = date(2026, 11, 1)
D2 = date(2026, 11, 2)
D3 = date(2026, 11, 3)


def ms(day: date, seconds: int = 0) -> int:
    return tape.Day(day).start_ms + seconds * 1000


# 2026-11-04 12:00 UTC: 11-03 has ended (and settled), 11-04 has not.
NOW = int(datetime(2026, 11, 4, 12, tzinfo=UTC).timestamp() * 1000)


class FakeVenue:
    """Serves /trades/hist like Bitfinex: rows with start <= mts <= end, sort=1, `limit` rows."""

    def __init__(self, rows: Sequence[Sequence[float | int]], limit: int = tape.PAGE_LIMIT,
                 fail_with: Exception | None = None) -> None:
        self.rows = sorted((list(r) for r in rows), key=lambda r: r[1])
        self.limit = limit
        self.fail_with = fail_with
        self.calls: list[tuple[str, int, int]] = []

    def __call__(self, symbol: str, start: int, end: int) -> list[list[float | int]]:
        self.calls.append((symbol, start, end))
        assert len(self.calls) <= 100, "paging does not converge"
        if self.fail_with is not None:
            raise self.fail_with
        return [r for r in self.rows if start <= r[1] <= end][: self.limit]


def rows_of(path: Path) -> list[tape.Row]:
    return tape.decode(path.read_bytes())


def run(root: Path, venue: FakeVenue, symbols: Sequence[str] = ("fUST",), start: date = D1,
        now: int = NOW) -> dict[str, int]:
    sleeps: list[float] = []
    result = tape.collect(root, symbols, start=start, fetch=venue, sleep=sleeps.append,
                          now_ms=lambda: now, log=lambda _m: None)
    assert all(s == tape.DEFAULT_PACE_S for s in sleeps)
    assert len(sleeps) == max(len(venue.calls) - 1, 0)  # every request after the first waits
    return result


def test_writes_one_sorted_file_per_finished_day_and_keeps_the_sign(tmp_path: Path) -> None:
    venue = FakeVenue([
        [11, ms(D1, 5), -100.5, 0.0002, 2],
        [10, ms(D1, 5), 50.0, 0.0003, 30],   # same mts, lower id: sorted by (mts, id)
        [12, ms(D2, 1), -7.25, 0.0001, 120],
        [13, ms(date(2026, 11, 4), 1), -1.0, 0.0001, 2],  # today: never written
    ])
    assert run(tmp_path, venue) == {"fUST": 3}
    assert rows_of(tape.day_path(tmp_path, "fUST", D1)) == [
        (10, ms(D1, 5), 50.0, 0.0003, 30), (11, ms(D1, 5), -100.5, 0.0002, 2)]
    assert rows_of(tape.day_path(tmp_path, "fUST", D2))[0][2] == -7.25
    assert rows_of(tape.day_path(tmp_path, "fUST", D3)) == []  # empty day still recorded
    assert not tape.day_path(tmp_path, "fUST", date(2026, 11, 4)).exists()


def test_a_day_is_not_written_until_it_has_settled(tmp_path: Path) -> None:
    just_after_midnight = int(datetime(2026, 11, 4, 0, 30, tzinfo=UTC).timestamp() * 1000)
    run(tmp_path, FakeVenue([]), now=just_after_midnight)
    assert sorted(p.name for p in (tmp_path / "fUST").iterdir()) == [
        "2026-11-01.csv.gz", "2026-11-02.csv.gz"]


def test_the_resume_point_comes_from_the_directory(tmp_path: Path) -> None:
    run(tmp_path, FakeVenue([]), now=int(datetime(2026, 11, 2, 12, tzinfo=UTC).timestamp() * 1000))
    venue = FakeVenue([[1, ms(D2, 1), -1.0, 0.0001, 2]])
    assert run(tmp_path, venue) == {"fUST": 2}
    assert [c[1] for c in venue.calls] == [ms(D2), ms(D3)]  # 11-01 not asked again


def test_a_gap_in_the_middle_is_filled(tmp_path: Path) -> None:
    run(tmp_path, FakeVenue([]))
    tape.day_path(tmp_path, "fUST", D2).unlink()
    venue = FakeVenue([])
    assert run(tmp_path, venue) == {"fUST": 1}
    assert [c[1] for c in venue.calls] == [ms(D2)]


def test_a_rerun_never_rewrites_an_existing_day(tmp_path: Path) -> None:
    run(tmp_path, FakeVenue([[1, ms(D1, 1), -1.0, 0.0001, 2]]))
    before = tape.day_path(tmp_path, "fUST", D1).read_bytes()
    venue = FakeVenue([[1, ms(D1, 1), -999.0, 0.0001, 2]])
    assert run(tmp_path, venue) == {"fUST": 0}
    assert venue.calls == []
    assert tape.day_path(tmp_path, "fUST", D1).read_bytes() == before
    with pytest.raises(FileExistsError):
        tape.write_day(tape.day_path(tmp_path, "fUST", D1), [])


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
    venue = FakeVenue(rows, limit=4)
    tape.collect(tmp_path, ["fUST"], start=D1, fetch=venue, sleep=lambda _s: None,
                 now_ms=lambda: int(datetime(2026, 11, 2, 12, tzinfo=UTC).timestamp() * 1000),
                 log=lambda _m: None)
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
        tape.fetch_day("fUST", D1, FakeVenue(rows, limit=3), lambda _s: None, 0)


def test_the_first_429_stops_the_run_and_keeps_finished_days(tmp_path: Path) -> None:
    good = FakeVenue([])
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
    assert [p.name for p in (tmp_path / "fUST").iterdir()] == ["2026-11-01.csv.gz"]


def test_main_exits_3_on_429(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tape, "http_fetch", FakeVenue([], fail_with=tape.RateLimitedError("x")))
    assert tape.main(["--tape-root", str(tmp_path), "--start", "2026-11-01"]) == 3


def test_http_fetch_does_not_retry_a_429(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error
    from email.message import Message

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
    run(tmp_path, FakeVenue([]))
    assert sorted(p.name for p in (tmp_path / "fUST").iterdir()) == [
        "2026-11-01.csv.gz", "2026-11-02.csv.gz", "2026-11-03.csv.gz"]


def test_verify_passes_on_identical_trades_and_never_writes(tmp_path: Path) -> None:
    trades = [[1, ms(D1, 1), -1.0, 0.0001, 2]]
    run(tmp_path, FakeVenue(trades))
    path = tape.day_path(tmp_path, "fUST", D1)
    stat = path.stat()
    assert tape.verify(tmp_path, ["fUST"], [D1, D2], fetch=FakeVenue(trades),
                       sleep=lambda _s: None, log=lambda _m: None) == 2
    assert path.stat().st_mtime_ns == stat.st_mtime_ns


@pytest.mark.parametrize("changed", [
    [[1, ms(D1, 1), -1.0, 0.0002, 2]],                                  # same count, other rate
    [[1, ms(D1, 1), -1.0, 0.0001, 2], [2, ms(D1, 2), -1.0, 0.0001, 2]],  # one more trade
], ids=["content", "count"])
def test_verify_refuses_a_different_day_without_overwriting(
    tmp_path: Path, changed: list[list[float | int]],
) -> None:
    run(tmp_path, FakeVenue([[1, ms(D1, 1), -1.0, 0.0001, 2]]))
    before = tape.day_path(tmp_path, "fUST", D1).read_bytes()
    with pytest.raises(tape.TapeMismatchError):
        tape.verify(tmp_path, ["fUST"], [D1], fetch=FakeVenue(changed),
                    sleep=lambda _s: None, log=lambda _m: None)
    assert tape.day_path(tmp_path, "fUST", D1).read_bytes() == before


def test_import_splits_a_spike_csv_into_the_same_files_a_fetch_writes(tmp_path: Path) -> None:
    trades = [[11, ms(D1, 5), -100.5, 0.0002, 2], [10, ms(D1, 5), 50.0, 0.0003, 30],
              [12, ms(D2, 1), -7.25, 0.0001, 120], [13, ms(date(2026, 11, 4), 1), -1.0, 0.0001, 2]]
    fetched, imported = tmp_path / "fetched", tmp_path / "imported"
    run(fetched, FakeVenue(trades), start=D1)
    lines = ["id,mts,amount,rate,period\n",
             *(f"{t[0]},{t[1]},{t[2]!r},{t[3]!r},{t[4]}\n" for t in trades)]
    assert tape.import_csv(imported, "fUST", lines, now_ms=NOW, log=lambda _m: None) == 2
    for day in (D1, D2):
        assert tape.day_path(imported, "fUST", day).read_bytes() == \
            tape.day_path(fetched, "fUST", day).read_bytes()
    assert not tape.day_path(imported, "fUST", date(2026, 11, 4)).exists()  # unfinished
    # A day with no trades has no line in the CSV; the next collect fills it from the API.
    assert tape.missing_days(imported, "fUST", D1, NOW) == [D3]


def test_import_keeps_existing_files_and_refuses_an_unordered_source(tmp_path: Path) -> None:
    run(tmp_path, FakeVenue([[1, ms(D1, 1), -1.0, 0.0001, 2]]), start=D1)
    before = tape.day_path(tmp_path, "fUST", D1).read_bytes()
    lines = [f"9,{ms(D1, 1)},-5.0,0.0001,2\n"]
    assert tape.import_csv(tmp_path, "fUST", lines, now_ms=NOW, log=lambda _m: None) == 0
    assert tape.day_path(tmp_path, "fUST", D1).read_bytes() == before
    unordered = [f"1,{ms(D1, 1)},-1.0,0.0001,2\n", f"2,{ms(D2, 1)},-1.0,0.0001,2\n",
                 f"3,{ms(D1, 2)},-1.0,0.0001,2\n"]
    with pytest.raises(ValueError, match="not ordered"):
        tape.import_csv(tmp_path / "x", "fUST", unordered, now_ms=NOW, log=lambda _m: None)
