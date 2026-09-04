"""SP5 plan gating — require_plan dependency (mechanism only; v1 wires nothing)."""
import pytest_asyncio
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.accounts.user_profile  # noqa: F401
from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.user_profile import UserProfile
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.plans import require_plan


def _profile(user_id: str, plan: str) -> UserProfile:
    return UserProfile(user_id=user_id, plan=plan)


@pytest_asyncio.fixture
async def make_client(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(_profile("user_free", "free"))
        s.add(_profile("user_pro", "pro"))
        await s.commit()

    def _make(user_id: str, minimum: str) -> TestClient:
        app = FastAPI()

        @app.get("/gated", dependencies=[Depends(require_plan(minimum))])
        async def gated() -> dict[str, str]:
            return {"ok": "1"}

        async def _fake_user():
            return Principal(user_id=user_id, email="w@e.com", role="operator")

        async def _override_session():
            async with factory() as s:
                yield s

        app.dependency_overrides[require_operator] = _fake_user
        app.dependency_overrides[get_session] = _override_session
        return TestClient(app)

    return _make


def test_equal_tier_passes(make_client) -> None:
    assert make_client("user_free", "free").get("/gated").status_code == 200


def test_below_tier_403(make_client) -> None:
    resp = make_client("user_free", "pro").get("/gated")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "plan_required"


def test_higher_tier_passes(make_client) -> None:
    assert make_client("user_pro", "free").get("/gated").status_code == 200


def test_missing_profile_row_defaults_to_free(make_client) -> None:
    assert make_client("user_ghost", "free").get("/gated").status_code == 200
    assert make_client("user_ghost", "pro").get("/gated").status_code == 403
