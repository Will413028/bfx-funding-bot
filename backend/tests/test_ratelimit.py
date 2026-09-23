"""SP5 rate limiting — pure token bucket + FastAPI dependency wiring."""
from decimal import Decimal  # noqa: F401  (parity with sibling test modules)

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.modules.api.ratelimit import (
    TokenBucketLimiter,
    build_rate_limit_dependency,
)

# ---------------------------------------------------------------------------
# Pure bucket
# ---------------------------------------------------------------------------


def test_bucket_allows_up_to_capacity_then_blocks() -> None:
    now = [0.0]
    limiter = TokenBucketLimiter(read_per_min=3, write_per_min=2, clock=lambda: now[0])
    for _ in range(3):
        assert limiter.acquire("u1", "read") is None
    retry = limiter.acquire("u1", "read")
    assert retry is not None and retry > 0


def test_bucket_refills_over_time() -> None:
    now = [0.0]
    limiter = TokenBucketLimiter(read_per_min=60, write_per_min=1, clock=lambda: now[0])
    for _ in range(60):
        assert limiter.acquire("u1", "read") is None
    assert limiter.acquire("u1", "read") is not None
    now[0] += 2.0  # 60/min → one token per second
    assert limiter.acquire("u1", "read") is None


def test_bucket_read_write_scopes_and_users_isolated() -> None:
    now = [0.0]
    limiter = TokenBucketLimiter(read_per_min=1, write_per_min=1, clock=lambda: now[0])
    assert limiter.acquire("u1", "read") is None
    assert limiter.acquire("u1", "write") is None   # separate scope
    assert limiter.acquire("u2", "read") is None    # separate user
    assert limiter.acquire("u1", "read") is not None


# ---------------------------------------------------------------------------
# Dependency wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    limiter = TokenBucketLimiter(read_per_min=2, write_per_min=1, clock=None)
    router = APIRouter(dependencies=[Depends(build_rate_limit_dependency(limiter))])

    @router.get("/thing")
    async def get_thing() -> dict[str, str]:
        return {"ok": "r"}

    @router.post("/thing")
    async def post_thing() -> dict[str, str]:
        return {"ok": "w"}

    app = FastAPI()
    app.include_router(router)

    async def _fake_user():
        return Principal(user_id="user_abc", email="w@e.com", role="operator")

    app.dependency_overrides[require_operator] = _fake_user
    return TestClient(app)


def test_read_limit_429_with_retry_after(client: TestClient) -> None:
    assert client.get("/thing").status_code == 200
    assert client.get("/thing").status_code == 200
    resp = client.get("/thing")
    assert resp.status_code == 429
    assert resp.json()["detail"] == "rate_limited"
    assert int(resp.headers["Retry-After"]) >= 1


def test_write_bucket_independent_of_read(client: TestClient) -> None:
    client.get("/thing")
    client.get("/thing")
    assert client.get("/thing").status_code == 429
    assert client.post("/thing").status_code == 200   # write bucket untouched
    assert client.post("/thing").status_code == 429   # write cap 1
