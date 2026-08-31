"""The epoch constant is asserted against hand-checked dates rather than trusted.

A wrong epoch shifts every window silently -- no exception, just wrong answers --
so this file is the one place the constant is allowed to be verified.
"""

from datetime import datetime, timezone

import pytest

from core import keepa_time as kt


def test_epoch_is_2011_01_01_utc():
    assert kt.keepa_to_datetime(0) == datetime(2011, 1, 1, tzinfo=timezone.utc)
    assert kt.KEEPA_EPOCH_SECONDS == 1_293_840_000


def test_known_timestamp_round_trips():
    # 2024-06-01T12:00:00Z, computed independently of the conversion helpers.
    dt = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
    minutes = kt.datetime_to_keepa(dt)
    assert minutes == int(dt.timestamp() // 60) - kt.KEEPA_EPOCH_MINUTES
    assert kt.keepa_to_datetime(minutes) == dt


def test_minute_granularity():
    assert kt.keepa_to_unix(1) - kt.keepa_to_unix(0) == 60


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        kt.datetime_to_keepa(datetime(2024, 6, 1, 12, 0))


def test_days_ago_uses_injected_now():
    assert kt.days_ago_keepa(90, now=1_000_000) == 1_000_000 - 90 * 1440


def test_london_formatting_applies_bst():
    """Series maths stays in UTC; only display converts. Confirm BST is applied
    at the boundary so an output timestamp is not an hour out in summer."""
    winter = kt.datetime_to_keepa(datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc))
    summer = kt.datetime_to_keepa(datetime(2024, 7, 15, 12, 0, tzinfo=timezone.utc))
    assert "12:00 GMT" in kt.format_london(winter)
    assert "13:00 BST" in kt.format_london(summer)
