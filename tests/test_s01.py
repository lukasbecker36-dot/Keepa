"""Tests for Strategy 1's analysis.

The distinguishing risk here is different from Strategy 2. S2 could recommend a
loss; S1 can only recommend wasted research time — but it locks up £500+ and a
quarter when the operator acts, so a false positive is expensive in a slower way.
The tests therefore lean on the rejection rules that keep defended listings out.
"""

import pytest

from core import csv_types, fees
from strategies import s01_niche_discovery as s01
from strategies.s01_niche_discovery import analyse

NOW = 10_000_000
DAY = 1440
START = NOW - s01.HISTORY_DAYS * DAY


def t(day_offset: float) -> int:
    return START + int(day_offset * DAY)


def make_product(
    *,
    asin="B0NICHE001",
    price_p=3200,
    rank=18_000,
    rank_avg90=19_000,
    reviews_now=140,
    reviews_90d_ago=134,
    sellers=2,
    monthly_sold=200,
    weight=480,
    sales_events=30,
    tracked_days_ago=400,
    title="Bamboo Drawer Organiser 4 Compartment",
    brand="Generic",
) -> dict:
    """A Keepa-shaped payload for a weakly-defended incumbent listing."""
    sales = [t(0), rank]
    for i in range(sales_events):
        at = (i + 1) * (s01.HISTORY_DAYS / (sales_events + 1))
        sales += [t(at), int(rank * 0.8), t(at + 0.4), rank]

    csv: list = [None] * 36
    csv[csv_types.NEW] = [t(0), price_p]
    csv[csv_types.SALES] = sales
    csv[csv_types.COUNT_NEW] = [t(0), sellers]
    csv[csv_types.COUNT_REVIEWS] = [
        t(0), max(reviews_90d_ago - 10, 0),
        t(s01.HISTORY_DAYS - 90), reviews_90d_ago,
        t(s01.HISTORY_DAYS - 1), reviews_now,
    ]

    stats = {"current": [-1] * 36, "avg90": [-1] * 36}
    stats["current"][csv_types.SALES] = rank
    stats["avg90"][csv_types.SALES] = rank_avg90
    stats["current"][csv_types.COUNT_REVIEWS] = reviews_now

    return {
        "asin": asin,
        "title": title,
        "brand": brand,
        "csv": csv,
        "stats": stats,
        "trackingSince": NOW - tracked_days_ago * DAY,
        "packageWeight": weight,
        "monthlySold": monthly_sold,
        "referralFeePercentage": 15.0,
        "categoryTree": [{"catId": 1, "name": "Storage & Organisation"}],
    }


# -- happy path -----------------------------------------------------------


def test_a_weak_incumbent_passes():
    a = analyse(make_product(), now=NOW)
    assert a.passed, a.rejected
    assert a.price_p == 3200
    assert a.reviews == 140
    assert a.sellers == 2
    assert 0 < a.score <= 100


def test_price_comes_from_observed_sales_not_listings():
    """Same lesson as Strategy 2: a listing price nobody pays is not a market."""
    a = analyse(make_product(price_p=4500), now=NOW)
    assert a.price_p == 4500
    assert a.sale_events >= s01.MIN_SALE_EVENTS


def test_max_viable_cost_is_the_actionable_number():
    """Profit is uncomputable here -- the sourcing cost is unknown. The inverse
    question has a definite answer."""
    a = analyse(make_product(price_p=3200), now=NOW)
    assert a.max_viable_cost_p == fees.max_viable_cost(
        3200, referral_pct=15.0, postage_p=fees.postage_for(480)
    )
    # Sanity: sourcing at that cost exactly clears the target.
    assert fees.profit(
        3200, a.max_viable_cost_p, referral_pct=15.0,
        postage_p=fees.postage_for(480),
    ) >= fees.MIN_PROFIT_PER_UNIT_P


# -- rejections -----------------------------------------------------------


def test_collapsing_demand_rejected():
    """Rank drifting far from its 90-day average means the niche is moving, and
    not necessarily toward you."""
    a = analyse(make_product(rank=40_000, rank_avg90=18_000), now=NOW)
    assert a.rejected.startswith("rank_unstable")


def test_too_few_sales_rejected():
    a = analyse(make_product(sales_events=5), now=NOW)
    assert a.rejected.startswith("only_")
    assert "sales_in_180d" in a.rejected


def test_crowded_asin_rejected():
    """A dozen sellers means the generic is already commoditised."""
    a = analyse(make_product(sellers=12), now=NOW)
    assert a.rejected == "12_sellers_already"


def test_fast_growing_reviews_rejected_as_defended():
    """The signal the brief's absolute review count cannot see: someone is
    actively pushing this listing and will fight for it."""
    a = analyse(make_product(reviews_90d_ago=40, reviews_now=190), now=NOW)
    assert a.rejected.startswith("reviews_growing")


def test_dormant_listing_is_not_penalised():
    a = analyse(make_product(reviews_90d_ago=138, reviews_now=140), now=NOW)
    assert a.passed
    assert a.review_velocity <= s01.DORMANT_VELOCITY


def test_missing_rank_data_rejected():
    p = make_product()
    p["stats"]["current"][csv_types.SALES] = -1
    assert analyse(p, now=NOW).rejected == "no_rank_data"


# -- scoring --------------------------------------------------------------


def test_score_terms_are_all_normalised():
    a = analyse(make_product(), now=NOW)
    for term in (a.stability, a.weakness, a.headroom, a.demand):
        assert 0.0 <= term <= 1.0


def test_price_above_the_ceiling_scores_zero_not_negative():
    """The brief's raw formula went negative past its boundary, so a £61 product
    scored worse than worthless."""
    a = analyse(make_product(price_p=5999), now=NOW)
    assert a.headroom >= 0.0
    assert a.score >= 0.0


def test_fewer_reviews_scores_higher():
    weak = analyse(make_product(reviews_now=30, reviews_90d_ago=28), now=NOW)
    strong = analyse(make_product(reviews_now=190, reviews_90d_ago=188), now=NOW)
    assert weak.score > strong.score


def test_review_velocity_penalty_separates_equal_review_counts():
    """Two listings with identical review counts, one dormant and one being
    pushed. The absolute count cannot tell them apart; velocity can."""
    dormant = analyse(make_product(reviews_now=150, reviews_90d_ago=148), now=NOW)
    pushed = analyse(make_product(reviews_now=150, reviews_90d_ago=120), now=NOW)
    assert dormant.reviews == pushed.reviews
    assert pushed.review_velocity > dormant.review_velocity
    assert dormant.score > pushed.score


def test_peer_context_replaces_own_review_count():
    group = [analyse(make_product(reviews_now=r, reviews_90d_ago=r - 2), now=NOW)
             for r in (20, 40, 60, 180)]
    s01.apply_peer_context(group)
    assert all(a.peer_avg_reviews is not None for a in group)
    assert group[0].peer_avg_reviews == pytest.approx(40.0)  # mean of 20/40/60


# -- output ---------------------------------------------------------------


def test_candidate_is_framed_as_research_not_a_buy():
    p = make_product()
    c = s01.to_candidate(p, analyse(p, now=NOW))
    assert "SOURCE BELOW" in c.notes
    assert "MOQ" in c.notes and "not a buy" in c.notes.lower()
    assert c.capital_required_p == 0, "MOQ is unknowable from Keepa; do not fake it"
    assert c.extra["alibaba_search"].startswith("https://www.alibaba.com/trade/search")


def test_alibaba_link_is_a_search_not_a_product():
    """CLAUDE.md: do not auto-source, do not scrape. A search URL only."""
    url = s01.alibaba_search_url(make_product(title="Bamboo Drawer Organiser, 4 Pack"))
    assert "SearchText=" in url
    assert "bamboo" in url.lower()


def test_brand_is_stripped_from_the_search_terms():
    url = s01.alibaba_search_url(
        {"title": "Acme Bamboo Drawer Organiser", "brand": "Acme"}
    )
    assert "acme" not in url.lower()


# -- the finder contract --------------------------------------------------


def test_weak_listing_fields_are_gates_not_score_inputs():
    """They are ProductFinderRequest-only and never returned on a payload, so
    they cannot be scored. Verified live against the API."""
    assert set(s01.WEAK_LISTING_FILTERS) == {"hasAPlus", "hasMainVideo", "imageCount_lte"}
    assert not set(s01.WEAK_LISTING_FILTERS) & set(s01.SELECTION)


def test_package_dimension_uses_the_volume_constant():
    """The lesson from Strategy 2, carried over rather than re-learned."""
    assert s01.SELECTION["packageDimension_lte"] == s01.MAX_PARCEL_VOLUME_MM3


def test_rating_is_required_and_priced_in():
    from core.client import RATING_EXTRA_PER_ASIN

    assert s01.TOKENS_PER_ASIN == 1 + RATING_EXTRA_PER_ASIN


# -- newcomer pricing: the ROK Straps lesson ------------------------------


def _pair(price_p: int, weight: int = 200):
    p = make_product(price_p=price_p, weight=weight)
    return p, analyse(p, now=NOW)


def test_brand_premium_row_is_dropped():
    """ROK Straps sells at £26.99 in a category whose generics clear at ~£10.
    That £15 gap is reputation, not product, and a no-name entrant cannot
    charge it -- so the apparent margin was never real."""
    pairs = [_pair(2699)]
    survivors, notes = s01.apply_newcomer_pricing(pairs, lambda cat: (1000, 12))
    assert survivors == []
    assert "brand premium" not in notes[0]  # note explains with numbers
    assert "above the category median" in notes[0]


def test_a_genuinely_generic_niche_survives():
    """Incumbent priced at the category median: no premium to lose, so the
    margin is real."""
    pairs = [_pair(3200)]
    survivors, _ = s01.apply_newcomer_pricing(pairs, lambda cat: (3200, 15))
    assert len(survivors) == 1
    a = survivors[0][1]
    assert a.achievable_price_p == 3200
    assert a.brand_premium == pytest.approx(1.0)


def test_pricing_never_assumes_above_the_incumbent():
    """If the category median is HIGHER than the incumbent, we still enter at
    the incumbent's price -- undercutting is the entrant's only lever."""
    pairs = [_pair(2600)]
    survivors, _ = s01.apply_newcomer_pricing(pairs, lambda cat: (4000, 10))
    assert survivors[0][1].achievable_price_p == 2600


def test_missing_peer_data_keeps_the_row_but_says_so():
    pairs = [_pair(3200)]
    survivors, _ = s01.apply_newcomer_pricing(pairs, lambda cat: (None, 0))
    assert len(survivors) == 1
    assert "no peer pricing" in survivors[0][1].notes_extra


def test_max_viable_cost_is_recomputed_from_the_achievable_price():
    pairs = [_pair(4000)]
    before = pairs[0][1].max_viable_cost_p
    survivors, _ = s01.apply_newcomer_pricing(pairs, lambda cat: (3000, 14))
    assert survivors, "3000 should still leave room to source"
    assert survivors[0][1].max_viable_cost_p < before
