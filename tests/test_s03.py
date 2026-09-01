"""Tests for Strategy 3's analysis.

`analyse()` is pure -- it takes a Keepa payload and spends nothing -- so the whole
reject sequence and the scoring pin against synthetic products without touching
the API, exactly as for Strategy 2.

The fixtures build a csv[1] NEW series as a list of (day, price) changes. The
default carries two completed dip->recovery cycles and a third, deeper dip that
is still open at `now` -- the shape the strategy exists to find.
"""

import pytest

from core import csv_types, fees
from strategies import s03_price_dip as s03
from strategies.s03_price_dip import analyse

NOW = 10_000_000
DAY = 1440
WINDOW_DAYS = s03.RANGE_DAYS
START = NOW - WINDOW_DAYS * DAY


def t(day_offset: float) -> int:
    return START + int(day_offset * DAY)


# Two prior dips to 2800 that recover to 4000, then a current, deeper dip to 2600
# still open at NOW.
DEFAULT_NEW = [(0, 4000), (30, 2800), (45, 4000), (80, 2800), (95, 4000), (170, 2600)]


def make_product(
    *,
    asin="B0TEST0003",
    new_points=DEFAULT_NEW,
    rank_points=((0, 20_000),),
    monthly_sold=500,
    weight=400,
    tracked_days_ago=280,
    title="Plain Kitchen Gadget",
    brand="Generic",
) -> dict:
    new: list = []
    for day, price in new_points:
        new += [t(day), price]
    sales: list = []
    for day, rank in rank_points:
        sales += [t(day), rank]

    csv: list = [None] * 36
    csv[csv_types.NEW] = new
    csv[csv_types.SALES] = sales

    return {
        "asin": asin,
        "title": title,
        "brand": brand,
        "csv": csv,
        "trackingSince": NOW - tracked_days_ago * DAY,
        "packageWeight": weight,
        "monthlySold": monthly_sold,
        "referralFeePercentage": 15.0,
        "categoryTree": [{"catId": 1, "name": "Kitchen"}],
    }


# -- the happy path -------------------------------------------------------


def test_a_clean_dip_passes_and_measures_correctly():
    a = analyse(make_product(), now=NOW)
    assert a.passed, a.rejected
    assert a.current_price_p == 2600
    assert a.reference_price_p == 4000     # 90-day median
    assert a.revert_price_p == 4000        # 180-day median (relist target)
    assert a.dip_pct == pytest.approx(0.35)
    assert a.recoveries == 2
    assert a.days_to_revert == pytest.approx(15.0)


def test_scoring_is_profit_delta_times_monthly_sales():
    a = analyse(make_product(), now=NOW)
    postage = fees.postage_for(400, None)
    expected_profit = fees.profit(4000, 2600, referral_pct=15.0, postage_p=postage)
    assert a.profit_per_unit_p == expected_profit
    assert a.expected_delta_p == 4000 - 2600
    assert a.score == pytest.approx(expected_profit * 500)


def test_days_to_revert_comes_from_prior_recovery_cycles():
    """Both prior cycles ran 15 days trough-to-recovery, so the estimate is 15."""
    a = analyse(make_product(), now=NOW)
    assert a.days_to_revert == pytest.approx(15.0)


# -- the falling-knife defence -------------------------------------------


def test_recent_structural_collapse_is_rejected_for_never_recovering():
    """THE key filter. A price that stepped down recently still reads as below
    its 90-day average -- the average has not yet caught up -- so a naive rule
    would buy it. Zero prior recoveries stops it."""
    collapse = [(0, 4000), (160, 2600)]  # drops once, near the end, never returns
    a = analyse(make_product(new_points=collapse), now=NOW)
    assert a.rejected == "only_0_recoveries"


def test_a_single_prior_recovery_is_an_anecdote_and_is_rejected():
    one_cycle = [(0, 4000), (80, 2800), (95, 4000), (170, 2600)]
    a = analyse(make_product(new_points=one_cycle), now=NOW)
    assert a.rejected == "only_1_recoveries"


# -- the reject sequence --------------------------------------------------


def test_insufficient_dip_rejected_against_the_average():
    mild = [(0, 4000), (30, 2800), (45, 4000), (80, 2800), (95, 4000), (170, 3700)]
    a = analyse(make_product(new_points=mild), now=NOW)
    assert a.rejected.startswith("dip_only_")


def test_flat_series_has_no_dip():
    a = analyse(make_product(new_points=[(0, 4000)]), now=NOW)
    assert a.rejected.startswith("dip_only_")


def test_narrow_range_rejected_even_when_below_average():
    """A dip below the average with too little volatility has nowhere to revert
    to; the move is noise, not a cycle."""
    narrow = [(0, 3400), (30, 2600), (45, 3400), (80, 2600), (95, 3400), (170, 2600)]
    a = analyse(make_product(new_points=narrow), now=NOW)
    assert a.rejected.startswith("range_too_narrow")


def test_below_average_but_not_near_the_low_is_rejected():
    """Down off the peak but well above the historical floor: the bottom of the
    cycle has not arrived, so the upside is unproven."""
    off_peak = [(0, 4000), (30, 2600), (45, 4000), (80, 2600), (95, 4000), (170, 3000)]
    a = analyse(make_product(new_points=off_peak), now=NOW)
    assert a.rejected == "not_near_range_low"


def test_demand_collapse_is_not_a_price_dip():
    """Same clean price dip, but the rank has ballooned at the current low --
    the market is shrinking, not on sale."""
    a = analyse(
        make_product(rank_points=((0, 20_000), (170, 200_000))), now=NOW
    )
    assert a.rejected.startswith("demand_collapsed")


def test_poor_average_rank_rejected():
    a = analyse(make_product(rank_points=((0, 400_000),)), now=NOW)
    assert a.rejected.startswith("rank_avg")


def test_thin_revert_delta_rejected_on_the_profit_floor():
    """A wide, recovering, in-demand cycle whose delta still does not clear the
    fixed handling costs after fees."""
    cheap = [(0, 2000), (30, 1400), (45, 2000), (80, 1400), (95, 2000), (170, 1400)]
    a = analyse(make_product(new_points=cheap), now=NOW)
    assert a.rejected.startswith("profit_")


def test_overweight_item_is_rejected_as_unpostable():
    a = analyse(make_product(weight=2500), now=NOW)
    assert a.rejected.startswith("unpostable")


def test_no_price_history_rejected():
    p = make_product()
    p["csv"][csv_types.NEW] = None
    assert analyse(p, now=NOW).rejected == "no_price_history"


def test_history_shorter_than_the_reference_window_is_rejected():
    a = analyse(make_product(tracked_days_ago=60), now=NOW)
    assert a.rejected == "history_too_short"


def test_sparse_history_rejected():
    p = make_product()
    # Data only starts three-quarters of the way through the window.
    p["csv"][csv_types.NEW] = [t(140), 4000, t(160), 2600]
    assert analyse(p, now=NOW).rejected == "sparse_history"


# -- volume handling ------------------------------------------------------


def test_missing_monthly_sold_is_flagged_not_silently_zeroed():
    a = analyse(make_product(monthly_sold=None), now=NOW)
    assert a.passed
    assert a.volume_is_nominal
    assert a.score > 0, "should still rank, just below anything with real volume"


def test_real_volume_outranks_nominal_volume():
    real = analyse(make_product(monthly_sold=500), now=NOW)
    nominal = analyse(make_product(monthly_sold=None), now=NOW)
    assert real.score > nominal.score


# -- candidate assembly ---------------------------------------------------


def test_candidate_carries_the_chart_link_and_the_reasoning():
    p = make_product()
    a = analyse(p, now=NOW)
    c = s03.to_candidate(p, a)
    assert c.keepa_url == "https://keepa.com/#!product/2-B0TEST0003"
    assert c.current_price_p == 2600
    assert c.capital_required_p == 2600 * s03.TEST_ORDER_UNITS
    assert c.days_to_realise == pytest.approx(15.0)
    assert "recovered 2x" in c.reason
    assert "relist" in c.reason
    assert c.extra["recoveries_180d"] == 2
    assert c.extra["dip_pct"] == pytest.approx(35.0, abs=0.1)
    assert c.extra["revert_price_p"] == 4000
    assert c.category == "Kitchen"


def test_notes_disclose_the_merchant_fulfilled_caveat():
    p = make_product()
    c = s03.to_candidate(p, analyse(p, now=NOW))
    assert "MERCHANT-FULFILLED" in c.notes


def test_nominal_volume_is_disclosed_in_the_notes():
    p = make_product(monthly_sold=None)
    c = s03.to_candidate(p, analyse(p, now=NOW))
    assert "nominal" in c.notes


# -- the finder query -----------------------------------------------------


def test_selection_bands_the_current_buy_price():
    assert s03.SELECTION["current_NEW_gte"] == s03.PRICE_FLOOR_P
    assert s03.SELECTION["current_NEW_lte"] == s03.PRICE_CEILING_P


def test_selection_prefilters_on_a_price_below_the_average():
    """deltaPercent90 is negative when current sits below the 90-day average."""
    assert s03.SELECTION["deltaPercent90_NEW_lte"] == -int(s03.MIN_DIP_PCT * 100)


def test_selection_requires_real_ongoing_demand():
    assert s03.SELECTION["avg90_SALES_lte"] == s03.MAX_RANK_AVG


def test_package_dimension_is_a_volume_not_a_length():
    assert s03.SELECTION["packageDimension_lte"] == s03.MAX_PARCEL_VOLUME_MM3
    assert s03.MAX_PARCEL_VOLUME_MM3 > 1_000_000


def test_recovery_band_has_hysteresis():
    """The low ceiling must sit below the recovery level, or jitter around one
    price would manufacture phantom cycles."""
    assert s03.MIN_DIP_PCT > s03.RECOVERY_TOLERANCE
