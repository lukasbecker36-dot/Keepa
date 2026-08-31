"""Strategy 1 — private-label niche discovery.

THE TRADE
    Find products selling steadily on Amazon UK where the incumbent listing is
    weak, then source an equivalent from Alibaba and out-list them.

    Unlike Strategy 2 this is NOT a specific-ASIN buy list. Each row names an
    ASIN as an EXEMPLAR of a niche; the action is to price that category on
    Alibaba, not to buy that ASIN. No auto-sourcing, and no scraping -- the
    output carries a search URL only.

    Economics are the opposite shape to Strategy 2, which changes how a score
    should be read. Strategy 2 chases a 15-20% spread and recycles capital in
    weeks. Strategy 1 chases a 4-6x markup but locks up £500+ for a quarter on
    MOQ and lead time. A weak row here deserves more attention than a strong row
    there.

WHAT THE API WILL AND WILL NOT GIVE BACK  (measured, not assumed)
    The weak-listing fields -- hasAPlus, hasMainVideo, imageCount,
    brandStoreName, variationCount, returnRate -- are ProductFinderRequest
    fields ONLY. They are never returned on a product payload, at any token
    cost. Verified live.

        => They must be GATES in pass 1. They cannot be score inputs, because
           there is no way to read them back per ASIN. The data settled that
           question rather than taste.

    Review data is absent from the free payload too: csv[16] RATING and csv[17]
    COUNT_REVIEWS are missing and stats.current[17] reads -1. Adding `rating=1`
    costs +1 token/ASIN (2 instead of 1) and returns full review history -- 126
    points on the ASIN tested. Worth paying: review count is the brief's core
    weakness signal, and review VELOCITY, which is a better signal, comes free
    once the history is there.

        => S1 costs 2 tokens/ASIN, not 1. Budget: 2 categories x 2 pages x 11 =
           44, plus ~100 ASINs x 2 = 200, so ~244/night.

    This is the third time an assumption about what arrives free has been wrong
    (csv[18] buy box, packageDimension units, and now these). Probe first.

THE WEAK-LISTING SIGNAL, which is the actual novelty
    The brief uses review count alone. Review count cannot tell a hobbyist from
    a serious operator: 180 reviews with full A+ content and a brand store is
    someone who will fight back; 140 reviews with four photos and no brand store
    is a drop-shipper who got lucky. Keepa can gate on the difference directly,
    and review VELOCITY separates a dormant listing from one being actively
    pushed -- which the absolute count cannot.

FOUR LESSONS CARRIED OVER FROM STRATEGY 2
    1. packageDimension is a VOLUME in cubic mm, not a longest side. 450
       returned zero results.
    2. Probe totalResults before committing to any new filter.
    3. Price ASKED is not price PAID. Validate against sales-rank drops, free.
    4. One measurement is not a pattern.

THE OUTPUT NUMBER THAT MATTERS: max viable cost
    Profit cannot be computed here -- the sale price is set by the incumbent,
    but the unit cost is whatever Alibaba quotes, which no API knows. So invert
    it: fees.max_viable_cost(sell_price) gives the highest landed cost that
    still clears the target. "Source below £12.40/unit or do not bother" is an
    instruction the operator can act on. A score is not.
"""

from __future__ import annotations

import math
import urllib.parse
from dataclasses import dataclass
import time
from statistics import mean, median
from typing import Callable

from core import csv_types, fees, filters
from core.client import KeepaClient, TokenBudgetExceeded
from core.series import ProductHistory, Window, price_at_sales
from strategies.base import Candidate, Excluded, ScanResult, Strategy
from strategies.s02_amazon_oos import MAX_PARCEL_VOLUME_MM3

# -- tunable thresholds ---------------------------------------------------

PRICE_FLOOR_P = 2500
PRICE_CEILING_P = 6000
MAX_CURRENT_RANK = 30_000
MAX_REVIEWS = 200
MAX_RANK_DRIFT = 0.30         # current vs 90d average
MIN_SALE_EVENTS = 20          # over the whole 180d window, not a gap
MAX_EXISTING_SELLERS = 8      # above this the generic is already commoditised
MAX_REVIEW_VELOCITY = 15.0    # reviews/month; above this the incumbent defends
DORMANT_VELOCITY = 2.0        # at or below this, nobody is pushing the listing
MIN_MONTHLY_SOLD = 30
DEMAND_REFERENCE_UNITS = 500  # normalises the demand term
ITEM_WEIGHT_LIMIT_G = 500
PACKAGE_WEIGHT_LIMIT_G = 700
MIN_RANK_DROP_PCT = 0.10

# -- newcomer pricing -----------------------------------------------------
# A score measures how weakly the INCUMBENT is defended. It silently assumed we
# inherit the incumbent's price, which is wrong whenever that price is brand
# premium. ROK Straps sells at GBP 26.99 in a category whose generics clear at
# GBP 8-19: that GBP 15 gap is reputation, not product, and a no-name entrant
# cannot charge it. Every max_viable_cost computed from an incumbent price was
# therefore optimistic.
#
# So price against the category's own median PAID price instead.
PEER_SAMPLE_SIZE = 20         # products sampled per leaf category
PEER_CACHE_DAYS = 7           # category price levels do not move by the hour
PEER_MIN_SALE_EVENTS = 5      # per peer, before its price is believed
NEWCOMER_PRICE_FACTOR = 1.0   # enter at the category median, not above it
BRAND_PREMIUM_FLAG = 1.5      # incumbent above this x median is trading on a name
MIN_VIABLE_LANDED_P = 200     # below GBP 2 landed, nothing is sourceable

HISTORY_DAYS = 180
FINDER_PAGES = 2
CATEGORIES_PER_NIGHT = 2
TOKENS_PER_ASIN = 2           # rating=1 is required; see the module docstring

SELECTION = {
    "current_BUY_BOX_SHIPPING_gte": PRICE_FLOOR_P,
    "current_BUY_BOX_SHIPPING_lte": PRICE_CEILING_P,
    "current_SALES_gte": 1,
    "current_SALES_lte": MAX_CURRENT_RANK,
    "current_COUNT_REVIEWS_lte": MAX_REVIEWS,
    "salesRankDrops90_gte": 30,
    "monthlySold_gte": MIN_MONTHLY_SOLD,
    "buyBoxIsAmazon": False,
    "itemWeight_lte": ITEM_WEIGHT_LIMIT_G,
    # packageWeight_lte alone matches products whose weight is UNKNOWN
    # (Keepa stores 0), which is how four 15-litre water bottles passed a
    # 700g filter. Requiring >=1g forces a real measurement.
    "packageWeight_gte": 1,
    "packageWeight_lte": PACKAGE_WEIGHT_LIMIT_G,
    "packageDimension_lte": MAX_PARCEL_VOLUME_MM3,
    "isHazMat": False,
    "batteriesRequired": False,
    "batteriesIncluded": False,
    "isAdultProduct": False,
    "isMerchOnDemand": False,
    "singleVariation": True,
    "productType": [0],
}

# Gates, not score inputs -- these are never returned on a product payload.
# Kept separate so a totalResults probe can price what each one costs us.
WEAK_LISTING_FILTERS = {
    "hasAPlus": False,
    "hasMainVideo": False,
    "imageCount_lte": 5,
}


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


@dataclass
class Analysis:
    asin: str
    rejected: str | None = None
    window: Window | None = None
    price_p: int = 0
    sale_events: int = 0
    rank_now: int = 0
    rank_avg90: int = 0
    rank_drift: float = 0.0
    reviews: int = 0
    review_velocity: float = 0.0
    sellers: int = 0
    monthly_sold: int = 0
    max_viable_cost_p: int = 0
    peer_avg_reviews: float | None = None
    peer_price_p: int = 0          # median PAID across the leaf category
    peer_sample: int = 0
    achievable_price_p: int = 0    # what a no-name entrant could charge
    brand_premium: float = 1.0     # incumbent price / category median
    notes_extra: str = ""
    stability: float = 0.0
    weakness: float = 0.0
    headroom: float = 0.0
    demand: float = 0.0
    score: float = 0.0

    @property
    def passed(self) -> bool:
        return self.rejected is None


def analyse(product: dict, *, now: int | None = None) -> Analysis:
    """Measure one incumbent listing. Pure; spends nothing; re-runnable on cache."""
    a = Analysis(asin=product.get("asin", ""))
    hist = ProductHistory.from_product(product)
    window = hist.window(HISTORY_DAYS, now=now)
    a.window = window

    stats = product.get("stats") or {}
    current = stats.get("current") or []
    avg90 = stats.get("avg90") or []

    def stat(table: list, idx: int) -> int:
        value = table[idx] if idx < len(table) else None
        return value if isinstance(value, int) else -1

    a.rank_now = stat(current, csv_types.SALES)
    a.rank_avg90 = stat(avg90, csv_types.SALES)
    if a.rank_now <= 0 or a.rank_avg90 <= 0:
        a.rejected = "no_rank_data"
        return a

    # A niche whose demand is collapsing is not one to enter. Not expressible in
    # the finder, so it happens here.
    a.rank_drift = abs(a.rank_avg90 - a.rank_now) / a.rank_avg90
    if a.rank_drift > MAX_RANK_DRIFT:
        a.rejected = f"rank_unstable_{a.rank_drift * 100:.0f}pct"
        return a

    # Price validated by transactions, not by listings -- the lesson from S2.
    new = hist[csv_types.NEW]
    sales = hist[csv_types.SALES]
    price, events = price_at_sales(
        new, sales, [window], min_drop_pct=MIN_RANK_DROP_PCT
    )
    a.sale_events = events
    if events < MIN_SALE_EVENTS:
        a.rejected = f"only_{events}_sales_in_180d"
        return a
    if not price:
        a.rejected = "no_price_at_sale_events"
        return a
    a.price_p = price

    a.sellers = hist[csv_types.COUNT_NEW].current() or 0
    if a.sellers > MAX_EXISTING_SELLERS:
        # The generic is already commoditised and you would be the ninth seller.
        a.rejected = f"{a.sellers}_sellers_already"
        return a

    reviews_series = hist[csv_types.COUNT_REVIEWS]
    a.reviews = max(
        reviews_series.current() or stat(current, csv_types.COUNT_REVIEWS), 0
    )
    then = reviews_series.at(window.end - 90 * 1440)
    if then is not None and then >= 0 and a.reviews >= then:
        a.review_velocity = (a.reviews - then) / 3.0     # per month over 90d
    if a.review_velocity > MAX_REVIEW_VELOCITY:
        # Being actively pushed. Whoever owns this listing will defend it.
        a.rejected = f"reviews_growing_{a.review_velocity:.0f}_per_month"
        return a

    a.monthly_sold = product.get("monthlySold") or 0
    a.max_viable_cost_p = fees.max_viable_cost(
        a.price_p,
        referral_pct=fees.referral_pct_for(product),
        is_media=fees.is_media_product(product),
        postage_p=fees.postage_for(product.get("packageWeight") or None),
    )
    if a.max_viable_cost_p <= 0:
        a.rejected = "price_cannot_support_a_margin"
        return a

    _score(a)
    return a


def _score(a: Analysis) -> None:
    """Normalised 0-1 factors, per PLAN.md 4.4.

    The brief's raw form goes negative past its boundaries, so a £61 product
    scored worse than worthless. Same multiplicative shape, each term clamped.
    """
    a.stability = clamp(1 - a.rank_drift / MAX_RANK_DRIFT)
    basis = a.peer_avg_reviews if a.peer_avg_reviews is not None else a.reviews
    weakness = clamp((MAX_REVIEWS - basis) / MAX_REVIEWS)
    # A dormant listing is beatable; one gaining reviews fast is defended.
    velocity_penalty = clamp(
        1 - 0.7 * (a.review_velocity / MAX_REVIEW_VELOCITY), 0.3, 1.0
    )
    a.weakness = weakness * velocity_penalty
    a.headroom = clamp(
        (PRICE_CEILING_P - a.price_p) / (PRICE_CEILING_P - PRICE_FLOOR_P)
    )
    a.demand = clamp(math.log1p(a.monthly_sold) / math.log1p(DEMAND_REFERENCE_UNITS))
    a.score = 100 * a.stability * a.weakness * a.headroom * a.demand


def apply_peer_context(analyses: list[Analysis]) -> None:
    """Set peer_avg_reviews from the three best-ranked peers in each group, then
    re-score. Implements the brief's 'avg review count of top 3' with no extra
    API spend, by grouping the scan's own results into £10 price bands."""
    groups: dict[int, list[Analysis]] = {}
    for a in analyses:
        groups.setdefault(a.price_p // 1000, []).append(a)
    for group in groups.values():
        best = sorted(group, key=lambda x: x.rank_now)[:3]
        peer = mean([b.reviews for b in best]) if best else None
        for a in group:
            a.peer_avg_reviews = peer
            _score(a)


def peer_price_lookup(client: KeepaClient) -> "Callable[[int], tuple[int | None, int]]":
    """Build a cached 'what does this leaf category actually sell for' lookup.

    One finder page plus PEER_SAMPLE_SIZE product fetches per category, cached
    for a week in the meta table. Prices come from price_at_sales, so this is
    the median PAID across the category -- not the median asked.
    """

    def lookup(category_id: int) -> tuple[int | None, int]:
        key = f"peer_price:{category_id}"
        cached = client.cache.get_meta(key)
        if cached and time.time() - cached.get("at", 0) < PEER_CACHE_DAYS * 86400:
            return cached.get("median_p"), cached.get("n", 0)

        selection = {
            "categories_include": [category_id],
            "current_BUY_BOX_SHIPPING_gte": 300,
            "current_BUY_BOX_SHIPPING_lte": PRICE_CEILING_P,
            "current_SALES_gte": 1,
            "current_SALES_lte": 120_000,
            "productType": [0],
            "sort": [["monthlySold", "desc"]],
        }
        try:
            asins = client.product_finder(selection)
            products = client.product(asins[:PEER_SAMPLE_SIZE], stats_days=HISTORY_DAYS)
        except TokenBudgetExceeded:
            return None, 0

        prices: list[int] = []
        for product in products.values():
            hist = ProductHistory.from_product(product)
            window = hist.window(HISTORY_DAYS)
            paid, events = price_at_sales(
                hist[csv_types.NEW], hist[csv_types.SALES], [window],
                min_drop_pct=MIN_RANK_DROP_PCT,
            )
            if paid and events >= PEER_MIN_SALE_EVENTS:
                prices.append(paid)
        median_p = int(median(prices)) if prices else None
        client.cache.set_meta(
            key, {"median_p": median_p, "n": len(prices), "at": time.time()}
        )
        return median_p, len(prices)

    return lookup


def apply_newcomer_pricing(
    pairs: list[tuple[dict, Analysis]],
    lookup: "Callable[[int], tuple[int | None, int]]",
) -> tuple[list[tuple[dict, Analysis]], list[str]]:
    """Re-price every candidate at what a NO-NAME entrant could actually charge.

    Returns the survivors and a note per row dropped. A row whose incumbent
    price turns out to be brand premium is removed: inheriting that price is
    the assumption that made it look viable in the first place.
    """
    survivors: list[tuple[dict, Analysis]] = []
    notes: list[str] = []
    for product, a in pairs:
        tree = product.get("categoryTree") or []
        leaf = tree[-1].get("catId") if tree else None
        if not leaf:
            survivors.append((product, a))
            continue

        peer_p, sample = lookup(leaf)
        a.peer_sample = sample
        if not peer_p:
            a.notes_extra = "no peer pricing available; incumbent price assumed"
            survivors.append((product, a))
            continue

        a.peer_price_p = peer_p
        a.brand_premium = a.price_p / peer_p if peer_p else 1.0
        # Enter at the category median, never above it, and never above what
        # the incumbent already charges.
        a.achievable_price_p = min(
            a.price_p, int(peer_p * NEWCOMER_PRICE_FACTOR)
        )
        a.max_viable_cost_p = fees.max_viable_cost(
            a.achievable_price_p,
            referral_pct=fees.referral_pct_for(product),
            is_media=fees.is_media_product(product),
            postage_p=fees.postage_for(product.get("packageWeight") or None),
        )
        if a.max_viable_cost_p < MIN_VIABLE_LANDED_P:
            notes.append(
                f"{a.asin} dropped: incumbent GBP {a.price_p/100:.2f} is "
                f"{(a.brand_premium - 1) * 100:.0f}% above the category median "
                f"GBP {peer_p/100:.2f}; at the median there is only "
                f"GBP {a.max_viable_cost_p/100:.2f} to source within"
            )
            continue
        _score(a)
        survivors.append((product, a))
    return survivors, notes


def alibaba_search_url(product: dict) -> str:
    """A SEARCH link only. No sourcing, no scraping -- CLAUDE.md is explicit."""
    title = (product.get("title") or "").split(",")[0]
    brand = (product.get("brand") or "").lower()
    words = [
        w for w in title.split()
        if len(w) > 2 and w.lower() != brand and w.isalpha()
    ][:5]
    query = urllib.parse.quote_plus(" ".join(words))
    return f"https://www.alibaba.com/trade/search?SearchText={query}"


def to_candidate(product: dict, a: Analysis) -> Candidate:
    tree = product.get("categoryTree") or []
    category = tree[-1].get("name", "") if tree else ""
    velocity = (
        "dormant"
        if a.review_velocity <= DORMANT_VELOCITY
        else f"{a.review_velocity:.0f} reviews/month"
    )
    return Candidate(
        asin=a.asin,
        title=product.get("title") or "",
        category=category,
        current_price_p=a.price_p,
        score=a.score,
        strategy="s01_niche_discovery",
        reason=(
            f"sells at £{a.price_p / 100:.2f} across {a.sale_events} observed "
            f"sales; {a.reviews} reviews ({velocity}); {a.sellers} seller(s); "
            f"rank {a.rank_now:,} stable within {a.rank_drift * 100:.0f}%"
        ),
        # MOQ is the real capital number, and it is not knowable from Keepa.
        capital_required_p=0,
        est_monthly_opportunity_p=a.max_viable_cost_p * a.monthly_sold // 100,
        days_to_realise=None,
        notes=(
            (f"Incumbent sells at £{a.price_p / 100:.2f} but the category median "
             f"is £{a.peer_price_p / 100:.2f}; priced as a no-name entrant at "
             f"£{a.achievable_price_p / 100:.2f}. " if a.peer_price_p else "")
            + (a.notes_extra + " " if a.notes_extra else "")
            + f"SOURCE BELOW £{a.max_viable_cost_p / 100:.2f}/unit landed to clear "
            f"£{fees.MIN_PROFIT_PER_UNIT_P / 100:.2f}/unit. Capital and lead time "
            f"are NOT modelled: Alibaba MOQ is typically 100-500 units (£500+) "
            f"with 6-10 weeks lead time before a single sale. Research lead, "
            f"not a buy."
        ),
        extra={
            "max_viable_landed_cost_p": a.max_viable_cost_p,
            "achievable_price_p": a.achievable_price_p or a.price_p,
            "category_median_paid_p": a.peer_price_p,
            "brand_premium_pct": round((a.brand_premium - 1) * 100, 1),
            "peer_sample": a.peer_sample,
            "sale_events_180d": a.sale_events,
            "reviews": a.reviews,
            "review_velocity_per_month": round(a.review_velocity, 1),
            "peer_avg_reviews": (
                round(a.peer_avg_reviews, 1) if a.peer_avg_reviews else ""
            ),
            "sellers": a.sellers,
            "rank_now": a.rank_now,
            "rank_drift_pct": round(a.rank_drift * 100, 1),
            "monthly_sold": a.monthly_sold,
            "stability": round(a.stability, 2),
            "weakness": round(a.weakness, 2),
            "headroom": round(a.headroom, 2),
            "demand": round(a.demand, 2),
            "alibaba_search": alibaba_search_url(product),
        },
    )


class NicheDiscoveryStrategy(Strategy):
    name = "s01_niche_discovery"
    description = "Weakly-defended Amazon UK niches worth sourcing from Alibaba"

    def __init__(
        self,
        client: KeepaClient,
        *,
        categories: list[int] | None = None,
        pages: int = FINDER_PAGES,
        weak_listing_gates: bool = True,
    ) -> None:
        self.client = client
        self.categories = categories or []
        self.pages = pages
        self.weak_listing_gates = weak_listing_gates

    def _selection(self, category: int | None) -> dict:
        sel = dict(SELECTION)
        if self.weak_listing_gates:
            sel.update(WEAK_LISTING_FILTERS)
        if category:
            sel["categories_include"] = [category]
        return sel

    def scan(self) -> ScanResult:
        notes = [
            "RESEARCH LEADS, NOT A BUY LIST. Each row names an ASIN as an "
            "EXEMPLAR of a niche; the action is to price that category on "
            "Alibaba, not to buy that ASIN.",
            "Capital is NOT modelled: MOQ is typically 100-500 units (£500+) "
            "with 6-10 weeks lead time. Read max_viable_landed_cost_p as the "
            "sourcing target that makes the niche worth entering.",
        ]
        excluded: list[Excluded] = []
        analyses: list[tuple[dict, Analysis]] = []
        start_spend = self.client.spent
        targets: list[int | None] = list(self.categories) or [None]

        for category in targets:
            try:
                asins = self.client.product_finder_pages(
                    self._selection(category), pages=self.pages
                )
                # rating=1 is required for review data: +1 token/ASIN.
                products = self.client.product(
                    asins, stats_days=HISTORY_DAYS, rating=True
                )
            except TokenBudgetExceeded as exc:
                notes.append(f"stopped early on category {category}: {exc}")
                break

            notes.append(f"category {category or 'unscoped'}: {len(asins)} ASINs")
            kept, dropped = filters.PRIVATE_LABEL.partition(products.values())
            for product, verdict in dropped:
                excluded.append(
                    Excluded(
                        product.get("asin", ""),
                        product.get("title") or "",
                        verdict.primary.filter_name if verdict.primary else "",
                        verdict.reason(),
                    )
                )
            for product in kept:
                analyses.append((product, analyse(product)))

        rejects: dict[str, int] = {}
        passing: list[tuple[dict, Analysis]] = []
        for product, a in analyses:
            if a.passed:
                passing.append((product, a))
            else:
                key = a.rejected.split(":")[0]
                rejects[key] = rejects.get(key, 0) + 1

        apply_peer_context([a for _, a in passing])
        # Re-price at what a no-name entrant could actually charge. This is the
        # step that removes rows whose apparent margin was the incumbent's brand
        # premium rather than a real gap.
        before = len(passing)
        passing, dropped_notes = apply_newcomer_pricing(
            passing, peer_price_lookup(self.client)
        )
        if before != len(passing):
            notes.append(
                f"newcomer pricing dropped {before - len(passing)} row(s) whose "
                f"margin was the incumbent's brand premium"
            )
            notes.extend(dropped_notes)
        candidates = [to_candidate(p, a) for p, a in passing]
        candidates.sort(key=lambda c: c.score, reverse=True)

        notes.append(
            f"{len(analyses)} analysed, {len(excluded)} excluded, "
            f"{len(candidates)} scored"
        )
        if rejects:
            notes.append(
                "rejects: "
                + ", ".join(
                    f"{k}={v}"
                    for k, v in sorted(rejects.items(), key=lambda kv: -kv[1])[:6]
                )
            )
        return ScanResult(
            candidates, excluded, self.client.spent - start_spend, notes
        )
