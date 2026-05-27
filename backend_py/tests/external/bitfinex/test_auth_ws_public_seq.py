from bfx_funding_bot.external.bitfinex.auth_ws import (
    Heartbeat,
    Unknown,
    _public_seq,
    parse_frame,
)


def test_public_seq_channel0_data_frame_takes_msg_seq_not_auth_seq():
    # [chan, type, payload, MSG_SEQ, AUTH_SEQ]
    msg = [0, "foc", [123, None, None], 77, 5]
    assert _public_seq(msg) == 77


def test_public_seq_heartbeat_frame_takes_last_int():
    # [chan, "hb", MSG_SEQ]
    assert _public_seq([0, "hb", 78]) == 78


def test_public_seq_data_frame_without_auth_seq_takes_last_int():
    assert _public_seq([0, "foc", [1, 2], 79]) == 79


def test_public_seq_no_seq_present_is_none():
    assert _public_seq([0, "foc", [1, 2]]) is None
    assert _public_seq([0, "hb"]) is None


def test_heartbeat_event_carries_public_seq():
    ev = parse_frame("[0,\"hb\",78]")
    assert isinstance(ev, Heartbeat)
    assert ev.raw_seq == 78


def test_unknown_channel_frame_carries_public_seq():
    # an unmodelled channel-0 frame (e.g. wallet update) still consumes a seq
    ev = parse_frame("[0,\"wu\",[\"funding\",\"USD\",100],90,6]")
    assert isinstance(ev, Unknown)
    assert ev.raw_seq == 90
