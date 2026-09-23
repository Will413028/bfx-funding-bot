from bfx_funding_bot.modules.lending.tracking.fill_rate import _percentile


def test_percentile_empty_is_none():
    assert _percentile([], 50.0) is None


def test_percentile_median_nearest_rank():
    assert _percentile([10, 20, 30], 50.0) == 20


def test_percentile_p90_nearest_rank():
    assert _percentile([10, 20, 30], 90.0) == 30


def test_percentile_single_value():
    assert _percentile([42], 50.0) == 42
