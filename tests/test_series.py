"""Tests for the csv[] decoder and time-weighted window maths.

Two failure modes get the most attention here, because both produce plausible
numbers rather than exceptions:

  1. Stride. csv[18] BUY_BOX_SHIPPING is [time, price, shipping] triplets while
     csv[0] AMAZON is pairs. Reading 18 with stride 2 interleaves shipping costs
     as timestamps.
  2. Sample- vs time-weighting. Keepa emits a point only on change, so a price
     held 40 days and one held 40 minutes are one sample each.
"""

import pytest

from core import csv_types
from core.series import (
    ProductHistory,
    Series,
    Window,
    is_missing,
    is_present,
)

DAY = 1440


def pairs(*tv: int) -> list[int]:
    return list(tv)


# -- stride ---------------------------------------------------------------


def test_pair_series_decodes_with_stride_two():
    s = Series.from_csv([0, 1000, DAY, 1200], csv_types.AMAZON)
    assert s.times == [0, DAY]
    assert s.values == [1000, 1200]


def test_shipping_series_decodes_as_triplets_and_combines():
    # [t=0, price=1000, ship=299], [t=DAY, price=1100, ship=0]
    s = Series.from_csv(
        [0, 1000, 299, DAY, 1100, 0], csv_types.BUY_BOX_SHIPPING
    )
    assert s.times == [0, DAY]
    assert s.values == [1299, 1100], "landed total should include shipping"
    assert s.shipping == [299, 0]


def test_shipping_can_be_left_uncombined():
    s = Series.from_csv(
        [0, 1000, 299], csv_types.BUY_BOX_SHIPPING, combine_shipping=False
    )
    assert s.values == [1000]


def test_unknown_shipping_treated_as_zero_not_subtracted():
    s = Series.from_csv([0, 1000, -1], csv_types.BUY_BOX_SHIPPING)
    assert s.values == [1000]


def test_missing_price_stays_missing_even_with_shipping():
    s = Series.from_csv([0, -1, 299], csv_types.BUY_BOX_SHIPPING)
    assert s.values == [-1]
    assert is_missing(s.values[0])


def test_stride_mismatch_raises_rather_than_silently_misreading():
    # Three entries cannot be a whole number of triplets.
    with pytest.raises(ValueError, match="stride"):
        Series.from_csv([0, 1000, 299, DAY], csv_types.BUY_BOX_SHIPPING)


def test_every_csv_type_has_a_known_stride():
    for idx, meta in csv_types.BY_INDEX.items():
        assert meta.stride in (2, 3)
        assert meta.stride == (3 if meta.is_with_shipping else 2)


def test_buy_box_shipping_is_a_triplet_type():
    """Guards the specific constant Strategy 2 depends on."""
    assert csv_types.BY_INDEX[csv_types.BUY_BOX_SHIPPING].stride == 3
    assert csv_types.BY_INDEX[csv_types.AMAZON].stride == 2


# -- step function semantics ---------------------------------------------


def test_at_returns_value_in_force():
    s = Series.from_csv([0, 100, 10 * DAY, 200], csv_types.AMAZON)
    assert s.at(-1) is None, "before first point there is no data"
    assert s.at(0) == 100
    assert s.at(5 * DAY) == 100
    assert s.at(10 * DAY) == 200
    assert s.at(999 * DAY) == 200, "last point extends forward"


def test_segments_clip_to_window_and_extend_last_point():
    s = Series.from_csv([0, 100, 10 * DAY, 200], csv_types.AMAZON)
    segs = list(s.segments(5 * DAY, 20 * DAY))
    assert [(x.start, x.end, x.value) for x in segs] == [
        (5 * DAY, 10 * DAY, 100),
        (10 * DAY, 20 * DAY, 200),
    ]


def test_empty_series_is_safe_to_query():
    s = Series.from_csv(None, csv_types.AMAZON)
    assert not s
    assert s.at(0) is None
    assert list(s.segments(0, DAY)) == []
    assert s.weighted_median(0, DAY) is None
    assert s.coverage(0, DAY) == 0.0


# -- the time-weighting property -----------------------------------------


def test_median_is_time_weighted_not_sample_weighted():
    """A price held 40 days vs three prices held minutes each.

    Sample-weighted, the median of [1000, 5000, 6000, 7000] is 5500. Time-
    weighted it is 1000, because that is what the product actually cost for
    essentially the whole window.
    """
    raw = [
        0, 1000,
        40 * DAY, 5000,
        40 * DAY + 20, 6000,
        40 * DAY + 40, 7000,
    ]
    s = Series.from_csv(raw, csv_types.AMAZON)
    assert s.weighted_median(0, 40 * DAY + 60) == 1000

    naive = sorted([1000, 5000, 6000, 7000])
    assert (naive[1] + naive[2]) / 2 == 5500  # what we are avoiding


def test_weighted_mean_also_respects_duration():
    s = Series.from_csv([0, 100, 9 * DAY, 200], csv_types.AMAZON)
    # 100 for 9 days, 200 for 1 day.
    assert s.weighted_mean(0, 10 * DAY) == pytest.approx(110.0)


def test_missing_values_excluded_from_price_aggregates():
    s = Series.from_csv([0, 1000, DAY, -1, 5 * DAY, 1200], csv_types.AMAZON)
    # The -1 stretch must not be averaged in as a price of -1.
    assert s.weighted_median(0, 10 * DAY) == 1200
    assert s.min_in(0, 10 * DAY) == 1000


def test_coverage_reports_usable_fraction():
    s = Series.from_csv([0, 1000, 5 * DAY, -1], csv_types.AMAZON)
    assert s.coverage(0, 10 * DAY) == pytest.approx(0.5)


# -- runs / gap detection -------------------------------------------------


def test_runs_finds_out_of_stock_windows():
    # In stock, then out for 10 days, then back.
    s = Series.from_csv(
        [0, 1000, 30 * DAY, -1, 40 * DAY, 1100], csv_types.AMAZON
    )
    gaps = s.runs(is_missing, 0, 60 * DAY)
    assert len(gaps) == 1
    assert gaps[0] == Window(30 * DAY, 40 * DAY)
    assert gaps[0].days == pytest.approx(10.0)


def test_min_minutes_drops_restock_blips():
    raw = [
        0, 1000,
        10 * DAY, -1,            # 2-hour blip
        10 * DAY + 120, 1000,
        20 * DAY, -1,            # real 12-day gap
        32 * DAY, 1000,
    ]
    s = Series.from_csv(raw, csv_types.AMAZON)
    all_gaps = s.runs(is_missing, 0, 40 * DAY)
    real_gaps = s.runs(is_missing, 0, 40 * DAY, min_minutes=24 * 60)
    assert len(all_gaps) == 2
    assert len(real_gaps) == 1
    assert real_gaps[0].days == pytest.approx(12.0)


def test_adjacent_qualifying_segments_merge_into_one_run():
    # Two consecutive missing values must read as a single gap, not two.
    s = Series.from_csv(
        [0, 1000, 10 * DAY, -1, 12 * DAY, -1, 20 * DAY, 1000], csv_types.AMAZON
    )
    gaps = s.runs(is_missing, 0, 30 * DAY)
    assert len(gaps) == 1
    assert gaps[0] == Window(10 * DAY, 20 * DAY)


def test_run_open_at_window_end_is_closed_at_the_boundary():
    s = Series.from_csv([0, 1000, 10 * DAY, -1], csv_types.AMAZON)
    gaps = s.runs(is_missing, 0, 15 * DAY)
    assert gaps == [Window(10 * DAY, 15 * DAY)]


def test_missing_fraction_matches_out_of_stock_percentage():
    """Should agree with the Finder's outOfStockPercentage90; a mismatch on real
    data means the window maths is wrong."""
    s = Series.from_csv([0, 1000, 27 * DAY, -1], csv_types.AMAZON)
    assert s.missing_fraction(0, 90 * DAY) == pytest.approx(0.7)
    assert s.runs(is_present, 0, 90 * DAY)[0].days == pytest.approx(27.0)


# -- the Strategy 2 shape -------------------------------------------------


def test_median_over_windows_weights_by_total_duration():
    """Strategy 2 wants the typical buy box price across all gaps combined --
    not the mean of per-gap medians, which would weight a 2-day gap the same as
    a 2-month one."""
    bb = Series.from_csv(
        [
            0, 2000, 0,
            10 * DAY, 3600, 0,     # during a long gap
            40 * DAY, 2000, 0,
            50 * DAY, 9000, 0,     # during a very short gap
            50 * DAY + 60, 2000, 0,
        ],
        csv_types.BUY_BOX_SHIPPING,
    )
    long_gap = Window(10 * DAY, 40 * DAY)
    short_gap = Window(50 * DAY, 50 * DAY + 60)
    assert bb.median_over_windows([long_gap, short_gap]) == 3600


def test_end_to_end_gap_pricing():
    """The core Strategy 2 measurement: Amazon's normal price vs the buy box
    price while Amazon is out."""
    product = {
        "asin": "B000TEST01",
        "csv": [None] * 36,
    }
    product["csv"][csv_types.AMAZON] = [
        0, 2400,
        60 * DAY, -1,          # Amazon out for 20 days
        80 * DAY, 2400,
    ]
    product["csv"][csv_types.BUY_BOX_SHIPPING] = [
        0, 2400, 0,
        60 * DAY, 3600, 0,     # buy box jumps while Amazon is away
        80 * DAY, 2400, 0,
    ]

    hist = ProductHistory.from_product(product)
    amazon = hist[csv_types.AMAZON]
    bb = hist[csv_types.BUY_BOX_SHIPPING]

    gaps = amazon.runs(is_missing, 0, 100 * DAY, min_minutes=24 * 60)
    assert len(gaps) == 1

    reference = amazon.weighted_median(0, 100 * DAY)
    gap_price = bb.median_over_windows(gaps)
    assert reference == 2400
    assert gap_price == 3600
    assert gap_price / reference == pytest.approx(1.5)


def test_absent_series_returns_empty_not_none():
    hist = ProductHistory.from_product({"asin": "X", "csv": [None] * 36})
    assert hist[csv_types.BUY_BOX_SHIPPING].weighted_median(0, DAY) is None
    assert csv_types.BUY_BOX_SHIPPING not in hist


# -- tracking-period clamping --------------------------------------------


def test_window_clamps_to_tracking_start():
    """A 90-day window on an ASIN tracked 56 days is a 56-day window.

    Found on the first live product tried: Keepa reported 100% Amazon OOS while
    we computed 62.6%, because we counted 34 untracked days as in-stock. Absence
    of data is not evidence of stock.
    """
    now = 10_000_000
    tracked_from = now - 56 * DAY
    hist = ProductHistory.from_product(
        {"asin": "B0TRACKED", "csv": [None] * 36, "trackingSince": tracked_from}
    )
    win = hist.window(90, now=now)
    assert win.start == tracked_from
    assert win.days == pytest.approx(56.0)
    assert hist.tracked_days(now=now) == pytest.approx(56.0)


def test_window_unclamped_when_history_is_long_enough():
    now = 10_000_000
    hist = ProductHistory.from_product(
        {"asin": "B0OLD", "csv": [None] * 36, "trackingSince": now - 900 * DAY}
    )
    assert hist.window(90, now=now).days == pytest.approx(90.0)


def test_window_survives_absent_tracking_since():
    now = 10_000_000
    hist = ProductHistory.from_product({"asin": "B0X", "csv": [None] * 36})
    assert hist.tracked_from is None
    assert hist.tracked_days(now=now) is None
    assert hist.window(90, now=now).days == pytest.approx(90.0)


def test_clamped_window_reproduces_keepas_percentage():
    """The live case that exposed the bug, reconstructed.

    Amazon's series has a single point -- 'no offer' -- at trackingSince. Over
    the tracked period that is 100% out of stock, which is what Keepa reports.
    """
    now = 10_000_000
    tracked_from = now - 56 * DAY
    hist = ProductHistory.from_product(
        {
            "asin": "B0GWHMLBWJ",
            "trackingSince": tracked_from,
            "csv": [[tracked_from, -1]] + [None] * 35,
        }
    )
    win = hist.window(90, now=now)
    amazon = hist[csv_types.AMAZON]
    assert amazon.missing_fraction(win.start, win.end) == pytest.approx(1.0)

    # The unclamped window is what produced the wrong 62.6%.
    naive = amazon.missing_fraction(now - 90 * DAY, now)
    assert naive == pytest.approx(56 / 90, abs=0.01)
