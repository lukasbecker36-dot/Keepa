"""Tests for Strategy 2's analysis.

`analyse()` is pure -- it takes a Keepa payload and spends nothing -- so the
whole reject sequence and the scoring can be pinned against synthetic products
without touching the API. That is also how thresholds get tuned in practice:
re-run over cached JSON for free.
"""

import pytest

from core import csv_types, fees
from strategies import s02_amazon_oos as s02
from strategies.s02_amazon_oos import analyse

NOW = 10_000_000
DAY = 1440
WINDOW_DAYS = s02.HISTORY_DAYS
START = NOW - WINDOW_DAYS * DAY


def t(day_offset: float) -> int:
    return START + int(day_offset * DAY)


def make_product(
    *,
    asin="B0TEST0001",
    gaps=((60, 75), (100, 115), (140, 155)),
    amazon_p=2400,
    gap_p=4000,
    rank=20_000,
    fba=0,
    monthly_sold=500,
    weight=400,
    tracked_days_ago=280,
    title="Plain Kitchen Gadget",
    brand="Generic",
    sales_per_gap=3,
) -> dict:
    """Build a Keepa-shaped payload with Amazon stock gaps.

    csv[0] AMAZON is -1 during each gap; csv[1] NEW rises to gap_p there.

    csv[3] SALES carries a sawtooth: each `sales_per_gap` event dips the rank by
    20%, which the strategy reads as a unit sold. Without those dips the gap
    price is an ask nobody paid, and the ASIN is correctly rejected -- so a
    fixture with a flat rank series would model an opportunity that does not
    exist.
    """
    amazon = [t(0), amazon_p]
    new = [t(0), amazon_p]
    sales = [t(0), rank]
    for start, end in gaps:
        amazon += [t(start), -1, t(end), amazon_p]
        new += [t(start), gap_p, t(end), amazon_p]
        span = end - start
        for i in range(sales_per_gap):
            at = start + span * (i + 1) / (sales_per_gap + 1)
            sales += [t(at), int(rank * 0.8), t(at + span * 0.05), rank]

    csv: list = [None] * 36
    csv[csv_types.AMAZON] = amazon
    csv[csv_types.NEW] = new
    csv[csv_types.SALES] = sales
    csv[csv_types.COUNT_NEW_FBA] = [t(0), fba]

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


def test_a_clean_candidate_passes_and_measures_correctly():
    a = analyse(make_product(), now=NOW)
    assert a.passed, a.rejected
    assert len(a.gaps) == 3
    assert a.mean_gap_days == pytest.approx(15.0)
    assert a.total_gap_days == pytest.approx(45.0)
    assert a.reference_price_p == 2400
    assert a.gap_price_p == 4000
    assert a.uplift == pytest.approx(4000 / 2400)


def test_scoring_is_profit_times_units_sellable_in_gaps():
    a = analyse(make_product(), now=NOW)
    # 45 gap-days over 180 days = 7.5 gap-days per month.
    # 500 units/month * (7.5/30) = 125 units sellable in gaps per month.
    assert a.units_in_gaps_per_month == pytest.approx(125.0)
    # Postage comes from the weight band, so the expectation must too.
    postage = fees.postage_for(400, None)
    assert a.profit_per_unit_p == fees.profit(
        4000, 2400, referral_pct=15.0, postage_p=postage
    )
    assert a.score == pytest.approx(a.profit_per_unit_p * 125.0)


def test_idle_days_measures_capital_dead_time():
    """Gaps at 60-75, 100-115, 140-155 leave 25-day waits between windows."""
    a = analyse(make_product(), now=NOW)
    assert a.idle_days == pytest.approx(25.0)


# -- the reject sequence --------------------------------------------------


def test_one_gap_is_an_anecdote_and_is_rejected():
    """The main defence against a supplier who has permanently exited."""
    a = analyse(make_product(gaps=((60, 120),)), now=NOW)
    assert a.rejected == "only_1_gaps"


def test_short_gaps_rejected_as_unsellable():
    """Amazon's restock is the exit deadline. Stock is already in hand so
    dispatch is same-day, but a gap of a few hours to a day cannot accumulate
    orders before it shuts."""
    a = analyse(make_product(gaps=((60, 61), (100, 101), (140, 141))), now=NOW)
    assert a.rejected.startswith("gaps_too_short")


def test_gaps_shorter_than_a_dispatch_cycle_are_still_sellable():
    """Buy-and-hold: a 4-day gap is workable because the stock is already here.
    An earlier 7-day floor rejected the strongest real candidate found."""
    a = analyse(make_product(gaps=((60, 64), (100, 104), (140, 144))), now=NOW)
    assert a.passed, a.rejected


def test_insufficient_uplift_rejected_against_the_scaled_threshold():
    a = analyse(make_product(gap_p=2700), now=NOW)  # only +12.5%
    assert a.rejected.startswith("uplift_")
    assert "below" in a.rejected


def test_flat_40_percent_would_have_passed_what_the_scaled_rule_rejects():
    """A £24 unit at +40% clears the brief's old rule but yields under £3/unit,
    so the scaled threshold correctly stops it."""
    gap = int(2400 * 1.40)
    assert gap / 2400 == pytest.approx(1.40)
    assert fees.profit(gap, 2400, referral_pct=15.0) < s02.MIN_PROFIT_PER_UNIT_P
    a = analyse(make_product(gap_p=gap), now=NOW)
    assert a.rejected.startswith("uplift_")


def test_high_rank_in_gaps_rejected_on_rank_level():
    """Secondary guard: even with sale events, a rank this poor is not a market
    worth holding stock for."""
    a = analyse(make_product(rank=400_000), now=NOW)
    assert a.rejected.startswith("rank_in_gaps")


# -- sales validation: price PAID, not price ASKED ------------------------


def test_a_price_nobody_paid_is_rejected():
    """The Crocs case, reduced. NEW showed £84.38 through two stock gaps with
    ZERO sales-rank drops in them -- one seller's ask with no market behind it.
    Scoring the asked price recommended a £18.44/unit loss as the best find in
    the scan."""
    a = analyse(make_product(gap_p=8438, sales_per_gap=0), now=NOW)
    assert a.rejected == "only_0_sales_in_gaps"


def test_too_few_sales_gives_no_basis_for_a_median():
    a = analyse(make_product(sales_per_gap=0, gaps=((60, 75),) * 1), now=NOW)
    assert a.rejected is not None


def test_gap_price_is_the_price_at_observed_sales():
    a = analyse(make_product(gap_p=4000), now=NOW)
    assert a.passed, a.rejected
    assert a.gap_price_p == 4000, "price in force when the rank dropped"
    assert a.sale_events == 9, "3 gaps x 3 sales each"


def test_asked_and_paid_are_reported_separately():
    """Both go in the CSV so a divergence is visible without re-running."""
    p = make_product()
    c = s02.to_candidate(p, analyse(p, now=NOW))
    assert c.extra["asked_price_p"] > 0
    assert c.extra["sale_events_in_gaps"] == 9
    assert "observed sales" in c.reason


def test_missing_amazon_history_rejected():
    p = make_product()
    p["csv"][csv_types.AMAZON] = None
    assert analyse(p, now=NOW).rejected == "no_amazon_history"


def test_no_third_party_price_during_gaps_rejected():
    p = make_product()
    p["csv"][csv_types.NEW] = None
    assert analyse(p, now=NOW).rejected == "no_third_party_price_in_gaps"


def test_overweight_item_is_rejected_as_unpostable():
    a = analyse(make_product(weight=2500), now=NOW)
    assert a.rejected.startswith("unpostable")


# -- competition is scored, not gated ------------------------------------


def test_fba_competition_applies_a_haircut_but_does_not_reject():
    """Per the operator's framing: the target is Amazon's stock state, not buy
    box ownership. Competitors reduce the realised price; they do not disqualify."""
    clean = analyse(make_product(gap_p=5000, fba=0), now=NOW)
    contested = analyse(make_product(gap_p=5000, fba=3), now=NOW)

    assert clean.passed and contested.passed
    assert clean.expected_sell_p == 5000
    assert contested.expected_sell_p == 2400 + int(2600 * s02.FBA_CONTESTED_HAIRCUT)
    assert contested.profit_per_unit_p < clean.profit_per_unit_p
    assert contested.score < clean.score


def test_haircut_applies_to_the_spread_not_the_absolute_price():
    """Multiplying the absolute price by 0.6 would price a £24 item at £14.40 --
    below cost -- and demand a +156% raw uplift before any contested row could
    pass, silently disqualifying all of them."""
    contested = analyse(make_product(gap_p=5000, fba=3), now=NOW)
    assert contested.expected_sell_p > contested.reference_price_p
    assert contested.expected_sell_p < 5000


def test_haircut_can_push_a_thin_candidate_below_the_profit_floor():
    """Passing on raw uplift but failing once the haircut is applied is a
    distinct rejection from failing the uplift test itself."""
    a = analyse(make_product(gap_p=3800, fba=4), now=NOW)
    assert a.rejected is not None
    assert a.rejected.startswith("profit_")


# -- window clamping ------------------------------------------------------


def test_window_clamps_to_tracking_since():
    """A young ASIN must be measured over the period Keepa actually tracked, or
    untracked days read as in-stock and the gap rate is understated."""
    a = analyse(make_product(tracked_days_ago=90), now=NOW)
    assert a.window.days == pytest.approx(90.0)


def test_history_shorter_than_two_sellable_gaps_is_rejected():
    a = analyse(make_product(tracked_days_ago=4), now=NOW)
    assert a.rejected == "history_too_short"


def test_sparse_history_rejected():
    """Coverage is measured with drop_missing=False -- we ask whether data
    exists across the window, not whether it was in stock. Using the default
    would reject exactly the high-OOS products this strategy looks for."""
    p = make_product()
    # Data only starts three-quarters of the way through the window.
    p["csv"][csv_types.AMAZON] = [t(140), 2400, t(150), -1, t(165), 2400]
    assert analyse(p, now=NOW).rejected == "sparse_history"


def test_a_heavily_out_of_stock_product_is_not_mistaken_for_sparse_data():
    """The bug this guards: an ASIN out of stock 60% of the time has 40% price
    coverage, and a naive coverage check would throw it away."""
    a = analyse(
        make_product(gaps=((20, 60), (80, 120), (140, 175))), now=NOW
    )
    assert a.passed, a.rejected
    assert a.total_gap_days > WINDOW_DAYS * 0.5


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
    c = s02.to_candidate(p, a)
    assert c.keepa_url == "https://keepa.com/#!product/2-B0TEST0001"
    assert c.capital_required_p == 2400 * s02.TEST_ORDER_UNITS
    assert "Amazon out 3x" in c.reason
    assert "needs +" in c.reason, "the required uplift belongs in the reason"
    assert c.extra["gaps_180d"] == 3
    assert c.extra["uplift_pct"] == pytest.approx(66.7, abs=0.1)
    assert c.category == "Kitchen"


def test_nominal_volume_is_disclosed_in_the_notes():
    p = make_product(monthly_sold=None)
    c = s02.to_candidate(p, analyse(p, now=NOW))
    assert "nominal" in c.notes


# -- the finder query -----------------------------------------------------


def test_selection_bands_amazons_own_price_not_the_current_buy_box():
    """If Amazon is out right now, the current buy box is the elevated gap
    price -- banding on it would band the wrong number."""
    assert "avg180_AMAZON_gte" in s02.SELECTION
    assert "current_BUY_BOX_SHIPPING_gte" not in s02.SELECTION


def test_selection_requires_amazon_to_have_held_the_buy_box():
    """Load-bearing: without it the query matches products Amazon has never
    stocked (100% OOS, one data point). Confirmed live."""
    assert s02.SELECTION["buyBoxStatsAmazon180_gte"] == 60


def test_price_floor_is_high_enough_to_clear_the_profit_target():
    """The floor must be a price at which the required uplift is achievable."""
    uplift = fees.required_uplift(s02.PRICE_FLOOR_P, s02.MIN_PROFIT_PER_UNIT_P)
    assert uplift < 1.60, "floor too low; needed uplift is unrealistic for gaps"


def test_package_dimension_is_a_volume_not_a_length():
    """Keepa's packageDimension is cubic millimetres. Setting it to 450 -- as if
    it were a longest side in mm -- filters to a volume smaller than a sugar
    cube and returns zero results. Confirmed live against totalResults."""
    assert s02.SELECTION["packageDimension_lte"] == s02.MAX_PARCEL_VOLUME_MM3
    assert s02.MAX_PARCEL_VOLUME_MM3 > 1_000_000, "a length would be ~450"


def test_longest_side_is_still_checked_locally():
    """Volume does not catch a long thin item, so the local filter must."""
    from core import filters
    long_thin = {"asin": "B0X", "title": "Curtain Pole", "brand": "Generic",
                 "packageLength": 1000, "packageWidth": 20, "packageHeight": 20}
    assert 1000 * 20 * 20 < s02.MAX_PARCEL_VOLUME_MM3, "passes the volume filter"
    assert filters.physical_limits(long_thin) is not None, "but must fail locally"


# -- proxy reliability ----------------------------------------------------


def test_gated_list_now_catches_the_brand_that_slipped_through():
    """A live scan surfaced Crocs as a top candidate; it is gated in practice
    and was missing from the list."""
    from core import filters
    assert filters.gated_brand(
        {"asin": "B0C4G674CY", "title": "Crocs Unisex Kids Classic Clog", "brand": "Crocs"}
    )


def test_selection_excludes_variation_children():
    """The one candidate that survived every other filter was a variation child
    whose NEW series disagreed with the buy box by 71%."""
    assert s02.SELECTION["singleVariation"] is True


def test_proxy_tolerance_is_a_noise_band_not_a_rounding_allowance():
    assert s02.PROXY_DISAGREEMENT_TOLERANCE >= 0.10
