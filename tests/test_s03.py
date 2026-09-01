"""Tests for Strategy 3's analysis.

`analyse()` is pure -- it takes a Keepa payload and spends nothing -- so the whole
reject sequence and the scoring pin against synthetic products without touching
the API, exactly as for Strategy 2.

The fixtures build a csv[1] NEW series as a list of (day, price) changes, and a
csv[3] SALES series carrying a sawtooth: each entry in `sale_days` dips the rank
20% (which the strategy reads as a unit sold) and recovers a day later. The
revert price is the price in force at those sales, so a fixture with no sales
above the dip has no proven reversion -- which is the whole point of the
paid-not-asked rule.
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

# Sale events, all in normal-price stretches of DEFAULT_NEW (0-30, 45-80, 95-170),
# so the price paid at each is 4000 -- the level the flip reverts to.
DEFAULT_SALE_DAYS = (10, 20, 60, 70, 110, 130, 150)


def make_product(
    *,
    asin="B0TEST0003",
    new_points=DEFAULT_NEW,
    sale_days=DEFAULT_SALE_DAYS,
    rank_base=20_000,
    rank_tail=None,
    monthly_sold=500,
    weight=400,
    tracked_days_ago=280,
    title="Plain Kitchen Gadget",
    brand="Generic",
) -> dict:
    new: list = []
    for day, price in new_points:
        new += [t(day), price]

    sales: list = [t(0), rank_base]
    for d in sale_days:
        sales += [t(d), int(rank_base * 0.8), t(d) + int(0.5 * DAY), rank_base]
    if rank_tail is not None:
        day, rank = rank_tail
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
    assert a.revert_price_p == 4000        # price at observed sales above the dip
    assert a.revert_sale_events == len(DEFAULT_SALE_DAYS)
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


# -- the revert price is PAID, not ASKED (the HP Poly lesson) --------------


def test_revert_requires_proven_sales_above_the_dip():
    """No sales anywhere means no demonstrated price to revert to, however high
    the asks stood. Mirrors Strategy 2's 'a price nobody paid' rejection."""
    a = analyse(make_product(sale_days=()), now=NOW)
    assert a.rejected == "only_0_sales_above_dip"


def test_sales_only_during_dips_do_not_prove_a_revert():
    """Units that sold only while the price was itself in a dip say nothing about
    the price it reverts to."""
    a = analyse(make_product(sale_days=(35, 85)), now=NOW)  # both in dip windows
    assert a.rejected.startswith("only_")
    assert a.rejected.endswith("_sales_above_dip")


def test_phantom_high_ask_is_not_scored_as_the_revert():
    """THE HP Poly case. A high price is ASKED for most of the window (Amazon out
    of stock, third parties asking high) but units change hands only at the lower
    normal level. The raw median of asks is the high phantom; the revert price
    must be the lower price that was actually paid."""
    phantom = [
        (0, 7000), (70, 4000), (80, 7000), (150, 4000), (160, 7000), (170, 2600)
    ]
    # Sales occur only in the two short 4000 stretches, never at 7000.
    a = analyse(
        make_product(new_points=phantom, sale_days=(72, 75, 152, 155)), now=NOW
    )
    assert a.passed, a.rejected
    assert a.revert_asked_p == 7000, "asks (time-weighted) reached the phantom high"
    assert a.revert_price_p == 4000, "but units only sold at the normal level"
    assert a.revert_price_p < a.revert_asked_p
    # Profit is computed off the paid price, not the phantom ask.
    postage = fees.postage_for(400, None)
    assert a.profit_per_unit_p == fees.profit(
        4000, 2600, referral_pct=15.0, postage_p=postage
    )


def test_paid_vs_asked_divergence_is_disclosed_in_the_notes():
    phantom = [
        (0, 7000), (70, 4000), (80, 7000), (150, 4000), (160, 7000), (170, 2600)
    ]
    p = make_product(new_points=phantom, sale_days=(72, 75, 152, 155))
    c = s03.to_candidate(p, analyse(p, now=NOW))
    assert "PAID price" in c.notes
    assert c.extra["revert_asked_p"] == 7000
    assert c.extra["revert_price_p"] == 4000


# -- the falling-knife defence -------------------------------------------


def test_recent_structural_collapse_is_rejected_for_never_recovering():
    """A price that stepped down recently still reads as below its 90-day
    average -- the average has not caught up -- so a naive rule would buy it. Zero
    prior recoveries stops it."""
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
    narrow = [(0, 3400), (30, 2600), (45, 3400), (80, 2600), (95, 3400), (170, 2600)]
    a = analyse(make_product(new_points=narrow), now=NOW)
    assert a.rejected.startswith("range_too_narrow")


def test_below_average_but_not_near_the_low_is_rejected():
    off_peak = [(0, 4000), (30, 2600), (45, 4000), (80, 2600), (95, 4000), (170, 3000)]
    a = analyse(make_product(new_points=off_peak), now=NOW)
    assert a.rejected == "not_near_range_low"


def test_demand_collapse_is_not_a_price_dip():
    """Same clean price dip and real prior sales, but the rank has ballooned at
    the current low -- the market is shrinking, not on sale."""
    a = analyse(make_product(rank_tail=(171, 200_000)), now=NOW)
    assert a.rejected.startswith("demand_collapsed")


def test_poor_average_rank_rejected():
    a = analyse(make_product(rank_base=400_000), now=NOW)
    assert a.rejected.startswith("rank_avg")


def test_thin_revert_delta_rejected_on_the_profit_floor():
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
    assert "observed sales" in c.reason
    assert c.extra["recoveries_180d"] == 2
    assert c.extra["dip_pct"] == pytest.approx(35.0, abs=0.1)
    assert c.extra["revert_price_p"] == 4000
    assert c.extra["revert_sale_events"] == len(DEFAULT_SALE_DAYS)
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
    assert s03.SELECTION["deltaPercent90_NEW_lte"] == -int(s03.MIN_DIP_PCT * 100)


def test_selection_requires_real_ongoing_demand():
    assert s03.SELECTION["avg90_SALES_lte"] == s03.MAX_RANK_AVG


def test_package_dimension_is_a_volume_not_a_length():
    assert s03.SELECTION["packageDimension_lte"] == s03.MAX_PARCEL_VOLUME_MM3
    assert s03.MAX_PARCEL_VOLUME_MM3 > 1_000_000


def test_recovery_band_has_hysteresis():
    assert s03.MIN_DIP_PCT > s03.RECOVERY_TOLERANCE
