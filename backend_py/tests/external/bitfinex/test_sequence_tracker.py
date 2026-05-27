from bfx_funding_bot.external.bitfinex.auth_ws import SequenceTracker


def test_first_observation_sets_baseline_no_gap():
    t = SequenceTracker()
    assert t.observe(10) == "ok"


def test_contiguous_is_ok():
    t = SequenceTracker()
    t.observe(10)
    assert t.observe(11) == "ok"
    assert t.observe(12) == "ok"


def test_forward_jump_is_gap():
    t = SequenceTracker()
    t.observe(10)
    assert t.observe(13) == "gap"  # 11, 12 dropped


def test_resync_after_gap_then_contiguous_is_ok():
    t = SequenceTracker()
    t.observe(10)
    t.observe(13)  # gap → baseline becomes 14
    assert t.observe(14) == "ok"


def test_none_seq_is_no_info_ok():
    t = SequenceTracker()
    t.observe(10)
    assert t.observe(None) == "ok"
    assert t.observe(11) == "ok"  # None did not disturb the baseline


def test_reset_clears_baseline_no_false_gap():
    t = SequenceTracker()
    t.observe(99)
    t.reset()  # new connection: seq restarts low
    assert t.observe(1) == "ok"
    assert t.observe(2) == "ok"


def test_backwards_seq_is_ok_and_rebaselines():
    t = SequenceTracker()
    t.observe(10)
    assert t.observe(8) == "ok"  # duplicate / reorder, not forward loss
    assert t.observe(9) == "ok"
    assert t.observe(11) == "ok"  # baseline never moved backward → no false gap
