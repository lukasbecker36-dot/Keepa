"""Tests for the fee model.

The arithmetic here decides whether every candidate in every strategy is
profitable, so the VAT direction is pinned explicitly: while unregistered, fee
VAT is a real cost and output VAT is not owed. Getting that backwards would
overstate margins on every row.
"""

import pytest

from core import fees
from core.fees import DEFAULT, FeeConfig, UnpostableError


# -- the unregistered position -------------------------------------------


def test_referral_fee_is_grossed_by_vat_while_unregistered():
    """15% headline is an 18% real cost. This is the multiplier that matters."""
    b = fees.net_proceeds(3600, referral_pct=15.0)
    assert b.referral_fee_p == 540          # 15% of £36.00
    assert b.fee_vat_p == 108               # 20% of the fee, unreclaimable
    assert b.referral_fee_p + b.fee_vat_p == 648   # = 18% of £36.00
    assert b.output_vat_p == 0, "no output VAT owed below the threshold"


def test_worked_example_from_the_plan():
    """The £24 -> £36 case: a 50% price jump nets £2.27 after flat costs."""
    b = fees.net_proceeds(3600, referral_pct=15.0)
    assert b.postage_p == 295
    assert b.packaging_p == 30
    assert b.net_p == 2627                          # £26.27
    assert b.profit_p(fees.landed_cost(2400)) == 227  # £2.27


def test_landed_cost_is_gross_while_unregistered():
    assert fees.landed_cost(3000) == 3000
    assert fees.landed_cost(3000, inbound_shipping_p=150) == 3150


def test_breakdown_sums_are_consistent():
    b = fees.net_proceeds(4999, referral_pct=15.0)
    assert b.net_p == b.sell_price_p - b.total_costs_p
    assert b.total_costs_p == (
        b.total_fees_p + b.output_vat_p + b.postage_p + b.packaging_p
    )


def test_minimum_referral_fee_applies_to_cheap_items():
    b = fees.net_proceeds(100, referral_pct=15.0)   # 15% of £1 = 15p
    assert b.referral_fee_p == fees.MIN_REFERRAL_FEE_P


def test_media_closing_fee_only_on_media():
    plain = fees.net_proceeds(2000, referral_pct=15.0)
    media = fees.net_proceeds(2000, referral_pct=15.0, is_media=True)
    assert plain.closing_fee_p == 0
    assert media.closing_fee_p == 75
    assert media.net_p < plain.net_p


def test_rounding_is_half_up_not_bankers():
    """round() would bias fee totals downward across a whole shortlist."""
    assert fees._round_p(0.5) == 1
    assert fees._round_p(1.5) == 2
    assert fees._round_p(2.5) == 3


# -- referral percentage from Keepa --------------------------------------


def test_referral_pct_prefers_the_keepa_value():
    assert fees.referral_pct_for({"referralFeePercentage": 8.0}) == 8.0


def test_referral_pct_falls_back_when_absent_or_junk():
    assert fees.referral_pct_for(None) == fees.DEFAULT_REFERRAL_PCT
    assert fees.referral_pct_for({}) == fees.DEFAULT_REFERRAL_PCT
    assert fees.referral_pct_for({"referralFeePercentage": 0}) == fees.DEFAULT_REFERRAL_PCT


# -- postage --------------------------------------------------------------


def test_postage_picks_the_cheapest_fitting_band():
    assert fees.postage_for(300, 300) == 155        # large letter
    assert fees.postage_for(1500, 400) == 295       # small parcel


def test_unknown_weight_does_not_assume_cheap():
    """Understating postage silently inflates every margin in the shortlist."""
    assert fees.postage_for(None) == fees.SMALL_PARCEL_P


def test_over_limit_items_are_rejected_not_priced():
    with pytest.raises(UnpostableError, match="2000g"):
        fees.postage_for(2500)
    with pytest.raises(UnpostableError, match="450mm"):
        fees.postage_for(500, 900)


# -- price-scaled thresholds ---------------------------------------------


def test_required_uplift_falls_as_price_rises():
    """The core justification for scaling: flat costs hurt cheap items most."""
    uplifts = [fees.required_uplift(p, 0) for p in (1500, 2200, 3000, 5000, 6000)]
    assert uplifts == sorted(uplifts, reverse=True)


@pytest.mark.parametrize(
    "cost_p, expected_uplift_pct",
    [(1500, 48), (2200, 40), (3000, 35), (5000, 30), (6000, 29)],
)
def test_breakeven_uplift_matches_the_documented_table(cost_p, expected_uplift_pct):
    got = (fees.required_uplift(cost_p, 0) - 1) * 100
    assert got == pytest.approx(expected_uplift_pct, abs=1.0)


def test_the_flat_40_percent_rule_loses_money_below_22_pounds():
    """The finding that motivated scaling: at the brief's £15 floor a 40% gap
    delta is a guaranteed loss, because breakeven there needs 48%."""
    assert fees.profit(int(1500 * 1.40), 1500) < 0
    assert fees.profit(int(2200 * 1.40), 2200) >= 0


def test_flat_40_percent_is_slack_at_higher_prices():
    """Above ~£45 a flat 40% demands more headroom than the economics need,
    so it discards workable candidates."""
    assert fees.profit(int(5000 * 1.40), 5000) > fees.MIN_PROFIT_PER_UNIT_P


def test_required_sell_price_is_exact_at_the_boundary():
    """Exactness matters: this is a filter threshold, so an off-by-one pence
    silently includes or drops candidates."""
    for cost in (1500, 2399, 3000, 5000, 7999):
        price = fees.required_sell_price(cost, fees.MIN_PROFIT_PER_UNIT_P)
        assert fees.profit(price, cost) >= fees.MIN_PROFIT_PER_UNIT_P
        assert fees.profit(price - 1, cost) < fees.MIN_PROFIT_PER_UNIT_P


def test_breakeven_is_the_zero_profit_boundary():
    for cost in (1500, 3000, 6000):
        be = fees.breakeven_sell_price(cost)
        assert fees.profit(be, cost) >= 0
        assert fees.profit(be - 1, cost) < 0


def test_target_profit_raises_the_required_price():
    assert fees.required_sell_price(3000, 300) > fees.required_sell_price(3000, 0)


def test_uplift_table_shape():
    rows = fees.uplift_table([1500, 3000])
    assert len(rows) == 2
    cost, breakeven, be_uplift, target_uplift = rows[0]
    assert cost == 1500
    assert breakeven > cost
    assert target_uplift > be_uplift


def test_uplift_needs_a_positive_cost():
    with pytest.raises(ValueError):
        fees.required_uplift(0)


# -- the VAT-registered future -------------------------------------------


def test_registering_for_vat_inverts_the_position():
    """Output VAT appears, fee VAT disappears, and input VAT becomes
    reclaimable. Net effect on a flip is strongly negative -- worth being able
    to model before crossing £90k."""
    reg = DEFAULT.registered()
    unreg_profit = fees.profit(3600, 2400)
    reg_profit = fees.profit(3600, 2400, cfg=reg)
    assert reg_profit < unreg_profit

    b = fees.net_proceeds(3600, referral_pct=15.0, cfg=reg)
    assert b.output_vat_p == 600      # 1/6 of £36.00
    assert b.fee_vat_p == 0           # reclaimable, so not a cost
    assert fees.landed_cost(2400, cfg=reg) == 2000  # input VAT recovered


def test_registered_thresholds_stay_exact():
    reg = DEFAULT.registered()
    price = fees.required_sell_price(3000, 300, cfg=reg)
    assert fees.profit(price, 3000, cfg=reg) >= 300
    assert fees.profit(price - 1, 3000, cfg=reg) < 300


# -- config plumbing ------------------------------------------------------


def test_config_is_overridable_without_touching_module_state():
    cheap_post = FeeConfig(postage_p=155)
    assert fees.profit(2000, 1000, cfg=cheap_post) > fees.profit(2000, 1000)
    assert DEFAULT.postage_p == fees.SMALL_PARCEL_P, "default must be unmutated"


def test_roi_reflects_capital_committed():
    b = fees.net_proceeds(3600, referral_pct=15.0)
    assert b.roi_pct(2400) == pytest.approx(227 / 2400 * 100, abs=0.01)


def test_explain_renders_the_unreclaimable_vat_line():
    text = fees.net_proceeds(3600, referral_pct=15.0).explain()
    assert "unreclaimable" in text
    assert "£36.00" in text


# -- importing, and why VAT registration flips sign -----------------------


def test_import_vat_is_unreclaimable_while_unregistered():
    """A 20% tax on the whole cost base that a registered competitor avoids."""
    unreg = fees.import_landed_cost(1000, freight_per_unit_p=200, duty_pct=0.0)
    reg = fees.import_landed_cost(1000, freight_per_unit_p=200, duty_pct=0.0,
                                  cfg=DEFAULT.registered())
    assert unreg == 1440   # (1000 + 200) * 1.20
    assert reg == 1200     # import VAT reclaimed
    assert unreg > reg


def test_max_fob_is_far_below_the_landed_ceiling():
    """The trap: a GBP 15.37 landed ceiling does NOT mean 'buy at GBP 15.37'.
    Freight, duty and unreclaimable import VAT all sit on top of the quote."""
    fob = fees.max_fob_price(1537, freight_per_unit_p=200, duty_pct=3.7)
    assert fob < 1100, "quoted price must be far below the landed ceiling"
    assert fees.import_landed_cost(fob, freight_per_unit_p=200,
                                   duty_pct=3.7) <= 1537


def test_registering_raises_the_affordable_fob_price():
    """The one place VAT registration helps: importing."""
    unreg = fees.max_fob_price(1537)
    reg = fees.max_fob_price(1537, cfg=DEFAULT.registered())
    assert reg > unreg
