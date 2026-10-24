"""The public proof site as a visitor sees it (https://borrowrate.pages.dev).

bfx-public-site.service builds and uploads it every week (ADR 2026-08-31 D6);
these tests check from outside, once a day in venue-contract.yml, that it still answers
and is not stale -- a weekly build that stopped happening, a revoked Pages token or a
broken upload all end here. Network, no credentials; never in a PR's CI.

The thresholds follow the weekly cadence (Monday's chain, plus a day of slack). fUSD is
checked for presence only: the database's fUSD hourly candles end at 2026-09-09 (measured
2026-10-24), so its CSV is history, not a feed, until that ingest resumes.
"""
import csv
import io
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.public_site]

SITE = "https://borrowrate.pages.dev"
WEEKLY = timedelta(days=9)
NEWEST_WEEK = timedelta(days=15)
# pages.dev refuses some default client agents (bfx_public_site.USER_AGENT).
HEADERS = {"User-Agent": "bfx-public-site-check/1", "Cache-Control": "no-cache"}


def _get(path: str) -> httpx.Response:
    response = httpx.get(f"{SITE}{path}", headers=HEADERS, timeout=30, follow_redirects=False)
    assert response.status_code == 200, f"{path}: HTTP {response.status_code}"
    return response


def test_the_proof_summary_is_this_weeks() -> None:
    body = json.loads(_get("/proof-summary.json").content)
    now = datetime.now(UTC)
    generated = datetime.fromisoformat(body["generated_at"])
    assert now - generated < WEEKLY, f"built {generated.isoformat()}: the weekly build or upload stopped"
    assert now - datetime.fromisoformat(body["as_of"]) < WEEKLY, "attribution has not been recomputed"
    assert body["weeks"], "the proof series is empty"
    newest = datetime.fromtimestamp(body["weeks"][-1]["week_start_ms"] / 1000, tz=UTC)
    assert generated - newest < NEWEST_WEEK, f"newest week {newest.date()}: the latest week is missing"


@pytest.mark.parametrize("path, disclaimer", [
    ("/", "過去績效不代表未來表現"),
    ("/en/", "Past performance is not indicative of future results"),
])
def test_the_pages_answer_with_the_disclaimer_and_the_csp(path: str, disclaimer: str) -> None:
    response = _get(path)
    assert disclaimer in response.text
    assert "default-src 'none'" in response.headers.get("content-security-policy", "")


def _rows(symbol: str) -> list[list[str]]:
    response = _get(f"/funding-rates-{symbol}.csv")
    assert "attachment" in response.headers.get("content-disposition", "")
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == ["date", "close_apr_pct"] and len(rows) > 1
    return rows[1:]


def test_the_fust_csv_reaches_this_week() -> None:
    last = datetime.strptime(_rows("fUST")[-1][0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert datetime.now(UTC) - last < WEEKLY, f"newest fUST hour {last.isoformat()}"


def test_the_fusd_csv_is_served() -> None:
    assert _rows("fUSD")
