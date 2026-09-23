from scripts.run_g3_live_validation import _default_capital


def test_capital_defaults_to_allocation_cap_env():
    assert _default_capital({"BFX_ALLOCATION_CAP_USDT": "10000"}) == "10000"


def test_capital_falls_back_without_env():
    # 歷史 fallback 570（僅 env 全缺時；VM .env.runtime 必有 cap）
    assert _default_capital({}) == "570"
