from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))

from g1_smoke_check import (  # type: ignore[import-not-found]
    build_apl_query_c2,
    run_c1_continuity,
)


def test_apl_query_c2_includes_required_filters():
    q = build_apl_query_c2(phase="paper", dataset="evt", hours=1)
    assert "phase == 'paper'" in q
    assert "event_type" in q and "signal" in q


async def test_c1_passes_when_all_buckets_non_empty():
    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value={
        "buckets": [{"_time": "t", "count": 5} for _ in range(12)],
    })
    result = await run_c1_continuity(client=fake_client, phase="paper", hours=1)
    assert result.passed is True


async def test_c1_fails_with_empty_bucket():
    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value={
        "buckets": [
            *[{"_time": "t", "count": 5} for _ in range(11)],
            {"_time": "t", "count": 0},
        ],
    })
    result = await run_c1_continuity(client=fake_client, phase="paper", hours=1)
    assert result.passed is False
