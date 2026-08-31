"""Strategy 2 — Amazon out-of-stock arbitrage.

THE TRADE
    Buy the unit FROM AMAZON while Amazon has it in stock and cheap. Hold. Sell
    when Amazon runs out and the market price rises. Source price and reference
    price are the same number, which is what makes this scoreable without any
    external cost data.

    The competitor that matters is Amazon's own inventory, not the buy box.
    While Amazon is in stock it holds the default offer and prices at will; when
    it goes out, that constraint lifts. `csv[0] == -1` encodes exactly this.

THREE PASSES
    1. Product Finder narrows server-side.        11 tokens/page, ~5 pages
    2. Free history reconstructs the real gaps.   1 token/ASIN, ~250 ASINs
    3. Paid buybox confirms the top N only.       3 tokens/ASIN, ~20 ASINs

    Pass 2 uses csv[1] NEW rather than csv[18] BUY_BOX_SHIPPING, because
    csv[18] needs the paid buybox flag.

    SALES VALIDATION -- the gap price is the price PAID, not the price ASKED.
    Each sales-rank drop is one unit sold, so the price in force at that instant
    is a transaction, not a listing. Taking the median over those events rather
    than over time is what separates a real market from one seller's hopeful ask.

    PROXY RELIABILITY -- read before trusting a row. On the first product
    measured the two series sat 1.6% apart, which looked like a safe
    substitution. On the first candidate that survived every filter they were
    71% apart, and in the dangerous direction: NEW implied a +141% gap uplift
    while the buy box showed -31%, i.e. a £18.44/unit LOSS rather than a
    £30.95/unit profit. csv[1] NEW is the lowest New offer excluding shipping
    and goes erratic exactly when Amazon is absent and few offers remain, while
    csv[18] is sparse and can carry a stale value through a gap.

    So pass 3 is not a formality. A row that fails buy-box verification is
    REMOVED from the shortlist, not annotated and left in place, and a large
    disagreement between the two series is reported as needing a manual chart
    check. The proxy is fit for ranking a wide scan cheaply; it is not fit for
    committing money.

OPERATIONAL CAVEAT, printed into every report
    This strategy sources FROM AMAZON, and Amazon retail receipts are NOT valid
    invoices for brand ungating. A gated ASIN is unlistable no matter how well
    it scores, so filters.RESALE is load-bearing here, not advisory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean

from core import csv_types, fees, filters
from core.client import KeepaClient, TokenBudgetExceeded
from core.series import ProductHistory, Window, is_missing, price_at_sales
from strategies.base import Candidate, Excluded, ScanResult, Strategy

# -- tunable thresholds ---------------------------------------------------
# Named constants at the top so tuning is one edit (CLAUDE.md working style).

PRICE_FLOOR_P = 2500          # £25 -- raised from the brief's £15; at £15 a unit
                              # needs a 73% gap uplift to clear £3, which Amazon
                              # gaps rarely produce. See fees.uplift_table().
PRICE_CEILING_P = 8000        # £80 -- capital ceiling on a £1,000 float
MIN_GAP_HOURS = 24            # shorter is a restock blip, not a selling window
MIN_GAPS_180D = 2             # one gap is an anecdote; two is a pattern
MIN_SELLABLE_GAP_DAYS = 3.0   # This is a BUY-AND-HOLD trade: stock is already in
                              # hand when the gap opens, so dispatch is same-day
                              # and the gap does not need to cover delivery time.
                              # It only needs to last long enough for orders to
                              # arrive. An earlier 7.0 was justified as "survives
                              # an MF dispatch cycle" -- wrong reasoning, and it
                              # excluded the strongest candidate found so far
                              # (+141% uplift over 5.7-day gaps). Dropping to 2.0
                              # admits one more real candidate; 3.0 is the
                              # conservative reading.
MAX_RANK_IN_GAP = 50_000      # secondary sanity check on rank LEVEL
MIN_RANK_DROP_PCT = 0.10      # a rank improvement this sharp counts as a sale
MIN_SALE_EVENTS_IN_GAPS = 3   # below this there is no basis for a price median
MIN_DATA_COVERAGE = 0.80      # of the 180d window, or the statistics are noise
FBA_CONTESTED_HAIRCUT = 0.6   # share of the gap PREMIUM captured when FBA
                              # sellers are also in the gap (not a price multiplier)
TEST_ORDER_UNITS = 5          # units per position, for capital_required
MIN_PROFIT_PER_UNIT_P = fees.MIN_PROFIT_PER_UNIT_P   # £3.00
VERIFY_TOP_N = 20             # pass 3 buybox confirmations
# How far csv[1] NEW may sit from the real csv[18] buy box before the row is
# treated as unreliable rather than merely imprecise. Measured 1.6% apart on one
# product and 71% apart on another, so this is a genuine noise band, not a
# rounding allowance -- see the PROXY note in the module docstring.
PROXY_DISAGREEMENT_TOLERANCE = 0.25
MISSING_VOLUME_UNITS = 1.0    # nominal monthly units when Keepa has no figure

# Keepa's packageDimension is a VOLUME in cubic millimetres, NOT a longest side.
# Determined empirically: packageDimension_lte=450 returned 0 results (450 mm3
# is smaller than a sugar cube), while 25,000,000 returned 287 of 297 and
# 1,000,000 returned 62 -- the signature of a volume filter.
# 45 x 35 x 16 cm is the Royal Mail small-parcel maximum.
MAX_PARCEL_VOLUME_MM3 = 450 * 350 * 160   # 25,200,000
# Volume alone does not stop a long thin item (1000x20x20mm is only 400,000mm3
# but a metre long), so filters.physical_limits() still checks the longest side
# locally from packageLength/Width/Height.

HISTORY_DAYS = 180
FINDER_PAGES = 5

SELECTION = {
    "buyBoxStatsAmazon180_gte": 60,
    "outOfStockPercentage90_gte": 20,
    # Above ~50% over 90 days Amazon has largely abandoned the ASIN, and those
    # show up as ONE long absence rather than a recurring pattern. On a live
    # page-1 sample, 9 of 27 analysed ASINs were rejected as only_1_gaps and
    # they clustered at the high-OOS end. 85 -> 50 cuts that tail server-side.
    "outOfStockPercentage90_lte": 50,
    # Amazon's NORMAL price -- our buy price. Deliberately not
    # current_BUY_BOX_SHIPPING: if Amazon is out right now, the current buy box
    # is the elevated gap price, so banding on it bands the wrong number.
    "avg180_AMAZON_gte": PRICE_FLOOR_P,
    "avg180_AMAZON_lte": PRICE_CEILING_P,
    "avg90_SALES_gte": 1,
    "avg90_SALES_lte": 50_000,
    "offerCountFBA_lte": 10,
    "monthlySold_gte": 10,
    # Requires a KNOWN weight: Keepa stores 0 for unknown, and 0 <= 2000.
    "packageWeight_gte": 1,
    "packageWeight_lte": 2000,
    "packageDimension_lte": MAX_PARCEL_VOLUME_MM3,
    "isHazMat": False,
    "productType": [0],
    # Variation children (one size/colour of a family) carry noisy price series,
    # because offers are attributed across the family. The one candidate that
    # survived every other filter on a live run was a variation child whose NEW
    # series disagreed with the buy box by 71%. 222 of 263 results survive this.
    "singleVariation": True,
    # Sorting by OOS% desc put the ABANDONED products first -- exactly the ones
    # MIN_GAPS_180D then rejects, so the first pages burned tokens on the worst
    # candidates. monthlySold is a valid sort field (confirmed live) and volume
    # is what drives the score.
    "sort": [["monthlySold", "desc"]],
}


@dataclass
class Analysis:
    """Everything measured for one ASIN. `rejected` is None if it survived."""

    asin: str
    rejected: str | None = None
    window: Window | None = None
    gaps: list[Window] = field(default_factory=list)
    reference_price_p: int = 0
    asked_price_p: int = 0        # time-weighted NEW median -- what was LISTED
    gap_price_p: int = 0          # price at observed sales -- what was PAID
    sale_events: int = 0
    expected_sell_p: int = 0
    uplift: float = 0.0
    required_uplift: float = 0.0
    rank_in_gaps: int = 0
    gap_competition: float = 0.0
    mean_gap_days: float = 0.0
    total_gap_days: float = 0.0
    idle_days: float | None = None
    profit_per_unit_p: int = 0
    units_in_gaps_per_month: float = 0.0
    monthly_sold: int = 0
    volume_is_nominal: bool = False
    score: float = 0.0

    @property
    def passed(self) -> bool:
        return self.rejected is None


def analyse(product: dict, *, now: int | None = None) -> Analysis:
    """Reconstruct Amazon's stock gaps and price the opportunity.

    Pure: takes a Keepa product payload, spends nothing, and can be re-run over
    cached JSON for free. All tuning happens here, against the cache.
    """
    asin = product.get("asin", "")
    a = Analysis(asin=asin)

    hist = ProductHistory.from_product(product)
    # Clamped to trackingSince: an unclamped window counts untracked days as
    # in-stock and understates the gap rate badly on younger ASINs.
    window = hist.window(HISTORY_DAYS, now=now)
    a.window = window
    if window.days < MIN_SELLABLE_GAP_DAYS * MIN_GAPS_180D:
        a.rejected = "history_too_short"
        return a

    amazon = hist[csv_types.AMAZON]
    new = hist[csv_types.NEW]
    sales = hist[csv_types.SALES]
    fba = hist[csv_types.COUNT_NEW_FBA]

    if not amazon:
        a.rejected = "no_amazon_history"
        return a

    # drop_missing=False on purpose: we are asking "is there recorded data across
    # the window", not "was it in stock". Using the default here would reject
    # exactly the high-OOS products this strategy exists to find.
    if amazon.coverage(window.start, window.end, drop_missing=False) < MIN_DATA_COVERAGE:
        a.rejected = "sparse_history"
        return a

    a.gaps = amazon.runs(
        is_missing, window.start, window.end, min_minutes=MIN_GAP_HOURS * 60
    )
    if len(a.gaps) < MIN_GAPS_180D:
        # One gap is an anecdote. This is the main defence against a supplier
        # who has permanently exited -- which looks identical to a cyclical
        # stockout until you check the chart.
        a.rejected = f"only_{len(a.gaps)}_gaps"
        return a

    a.total_gap_days = sum(g.days for g in a.gaps)
    a.mean_gap_days = a.total_gap_days / len(a.gaps)
    if a.mean_gap_days < MIN_SELLABLE_GAP_DAYS:
        # Amazon's restock is the exit deadline; a short window shuts before an
        # MF parcel lands.
        a.rejected = f"gaps_too_short_{a.mean_gap_days:.1f}d"
        return a

    # Idle capital: how long from one gap closing to the next opening.
    intervals = [
        (later.start - earlier.end) / 1440.0
        for earlier, later in zip(a.gaps, a.gaps[1:])
    ]
    a.idle_days = mean(intervals) if intervals else None

    reference = amazon.weighted_median(window.start, window.end)
    if not reference:
        a.rejected = "no_amazon_price"
        return a
    a.reference_price_p = reference

    a.asked_price_p = new.median_over_windows(a.gaps) or 0
    if not a.asked_price_p:
        a.rejected = "no_third_party_price_in_gaps"
        return a

    # What was PAID, not what was ASKED. Each sales-rank drop is a unit sold;
    # read the price in force at that instant and take the median of those.
    #
    # This is the single most important correction in the strategy. A
    # time-weighted median says an item was LISTED at £84 for most of a stock
    # gap; it cannot say anyone bought at £84. On the first real candidate the
    # asked price was £84.38 and the paid price £28.63 -- the two gaps showing
    # £84 had zero rank drops in them. Scoring the asked price would have
    # recommended a £18.44/unit loss as the best find in the scan.
    gap_price, events = price_at_sales(
        new, sales, a.gaps, min_drop_pct=MIN_RANK_DROP_PCT
    )
    a.sale_events = events
    if events < MIN_SALE_EVENTS_IN_GAPS:
        # Nothing measurably sold while Amazon was away. Whatever the listings
        # said, there is no demonstrated market at that price.
        a.rejected = f"only_{events}_sales_in_gaps"
        return a
    if not gap_price:
        a.rejected = "no_price_at_sale_events"
        return a
    a.gap_price_p = gap_price
    a.uplift = gap_price / reference

    referral_pct = fees.referral_pct_for(product)
    is_media = fees.is_media_product(product)
    try:
        postage_p = fees.postage_for(
            product.get("packageWeight") if product.get("packageWeight", -1) > 0 else None,
            None,
        )
    except fees.UnpostableError as exc:
        a.rejected = f"unpostable: {exc}"
        return a

    a.required_uplift = fees.required_uplift(
        reference,
        MIN_PROFIT_PER_UNIT_P,
        referral_pct=referral_pct,
        is_media=is_media,
        postage_p=postage_p,
    )
    if a.uplift < a.required_uplift:
        # The price-scaled threshold replacing the brief's flat 40%, which loses
        # money below £22 and is slack above £45.
        a.rejected = (
            f"uplift_{(a.uplift - 1) * 100:.0f}pct_below_"
            f"{(a.required_uplift - 1) * 100:.0f}pct"
        )
        return a

    rank = sales.median_over_windows(a.gaps)
    if rank is None or rank > MAX_RANK_IN_GAP:
        # Guards the classic trap: the price "rises" during a gap because
        # nothing is selling at the higher price.
        a.rejected = f"rank_in_gaps_{rank}"
        return a
    a.rank_in_gaps = rank

    # Competition is SCORED, not gated. With Amazon out and no FBA sellers you
    # are the only offer; with several you are a worse-fulfilment fourth offer
    # and will realise below the median gap price.
    a.gap_competition = fba.median_over_windows(a.gaps) or 0
    realism = 1.0 if a.gap_competition == 0 else FBA_CONTESTED_HAIRCUT
    # The haircut applies to the SPREAD, not the absolute price. Competition
    # erodes how much of the gap premium we capture; it does not drag the price
    # below what Amazon itself charges, since that is the floor the market sits
    # at when Amazon is present. Applying 0.6 to the absolute price instead
    # would mean selling a £24 item for £14.40 -- a guaranteed loss -- and would
    # demand a raw uplift of +156% before any contested row could pass, which
    # silently disqualifies every one of them.
    a.expected_sell_p = reference + int((gap_price - reference) * realism)

    a.profit_per_unit_p = fees.profit(
        a.expected_sell_p,
        reference,
        referral_pct=referral_pct,
        is_media=is_media,
        postage_p=postage_p,
    )
    if a.profit_per_unit_p < MIN_PROFIT_PER_UNIT_P:
        a.rejected = f"profit_{a.profit_per_unit_p}p_after_haircut"
        return a

    monthly_sold = product.get("monthlySold") or 0
    a.monthly_sold = monthly_sold
    gap_days_per_month = a.total_gap_days / (window.days / 30.0)
    if monthly_sold > 0:
        a.units_in_gaps_per_month = monthly_sold * (gap_days_per_month / 30.0)
    else:
        # Keepa has no "bought in past month" figure here. Rank a nominal one
        # unit rather than zero, so the row still appears but sorts below
        # anything with real volume data.
        a.volume_is_nominal = True
        a.units_in_gaps_per_month = MISSING_VOLUME_UNITS * (gap_days_per_month / 30.0)

    a.score = a.profit_per_unit_p * a.units_in_gaps_per_month
    return a


def _reason(a: Analysis) -> str:
    bits = [
        f"Amazon out {len(a.gaps)}x in {a.window.days:.0f}d (avg {a.mean_gap_days:.0f}d)",
        f"£{a.reference_price_p / 100:.2f} -> £{a.gap_price_p / 100:.2f} at "
        f"{a.sale_events} observed sales "
        f"(+{(a.uplift - 1) * 100:.0f}%, needs +{(a.required_uplift - 1) * 100:.0f}%)",
        f"£{a.profit_per_unit_p / 100:.2f}/unit",
    ]
    if a.gap_competition:
        bits.append(f"{a.gap_competition:.0f} FBA in gaps (price haircut applied)")
    else:
        bits.append("no FBA competition in gaps")
    return "; ".join(bits)


def _notes(a: Analysis) -> str:
    notes = []
    if a.volume_is_nominal:
        notes.append("no monthlySold figure; volume is nominal, ranking unreliable")
    if a.idle_days is not None:
        notes.append(f"capital idle ~{a.idle_days:.0f}d between gaps")
    notes.append(f"rank in gaps {a.rank_in_gaps:,}")
    return "; ".join(notes)


def to_candidate(product: dict, a: Analysis) -> Candidate:
    tree = product.get("categoryTree") or []
    category = tree[-1].get("name", "") if tree else ""
    return Candidate(
        asin=a.asin,
        title=product.get("title") or "",
        category=category,
        current_price_p=a.reference_price_p,
        score=a.score,
        strategy="s02_amazon_oos",
        reason=_reason(a),
        capital_required_p=a.reference_price_p * TEST_ORDER_UNITS,
        est_monthly_opportunity_p=int(a.score),
        days_to_realise=a.idle_days,
        notes=_notes(a),
        extra={
            "gaps_180d": len(a.gaps),
            "mean_gap_days": round(a.mean_gap_days, 1),
            "total_gap_days": round(a.total_gap_days, 1),
            "gap_price_p": a.gap_price_p,
            "asked_price_p": a.asked_price_p,
            "sale_events_in_gaps": a.sale_events,
            "expected_sell_p": a.expected_sell_p,
            "uplift_pct": round((a.uplift - 1) * 100, 1),
            "required_uplift_pct": round((a.required_uplift - 1) * 100, 1),
            "profit_per_unit_p": a.profit_per_unit_p,
            "units_in_gaps_per_month": round(a.units_in_gaps_per_month, 2),
            "monthly_sold": a.monthly_sold,
            "rank_in_gaps": a.rank_in_gaps,
            "fba_in_gaps": a.gap_competition,
            "volume_is_nominal": a.volume_is_nominal,
        },
    )


class AmazonOosStrategy(Strategy):
    name = "s02_amazon_oos"
    description = "Buy from Amazon in stock, sell into Amazon's stock gaps"

    def __init__(
        self,
        client: KeepaClient,
        *,
        pages: int = FINDER_PAGES,
        verify_top_n: int = VERIFY_TOP_N,
        selection: dict | None = None,
    ) -> None:
        self.client = client
        self.pages = pages
        self.verify_top_n = verify_top_n
        self.selection = selection or SELECTION

    def scan(self) -> ScanResult:
        notes = [
            "SOURCED FROM AMAZON: Amazon retail receipts are NOT valid ungating "
            "invoices, so gated brands are unlistable regardless of score.",
            "Gap prices use csv[1] NEW (free). Top rows re-checked against "
            "csv[18] buy box -- see buybox_verified in the CSV.",
        ]
        excluded: list[Excluded] = []
        candidates: list[Candidate] = []
        start_spend = self.client.spent

        try:
            asins = self.client.product_finder_pages(
                self.selection, pages=self.pages
            )
            notes.append(f"finder returned {len(asins)} ASINs")
            products = self.client.product(asins, stats_days=HISTORY_DAYS)
        except TokenBudgetExceeded as exc:
            notes.append(f"ABORTED during fetch: {exc}")
            return ScanResult([], excluded, self.client.spent - start_spend, notes)

        kept, dropped = filters.RESALE.partition(products.values())
        for product, verdict in dropped:
            excluded.append(
                Excluded(
                    asin=product.get("asin", ""),
                    title=product.get("title") or "",
                    filter_name=verdict.primary.filter_name if verdict.primary else "",
                    detail=verdict.reason(),
                )
            )

        rejects: dict[str, int] = {}
        for product in kept:
            a = analyse(product)
            if a.passed:
                candidates.append(to_candidate(product, a))
            else:
                key = a.rejected.split(":")[0]
                rejects[key] = rejects.get(key, 0) + 1

        candidates.sort(key=lambda c: c.score, reverse=True)
        notes.append(
            f"{len(kept)} passed filters, {len(excluded)} excluded, "
            f"{len(candidates)} scored"
        )
        if rejects:
            top = sorted(rejects.items(), key=lambda kv: -kv[1])[:6]
            notes.append("rejects: " + ", ".join(f"{k}={v}" for k, v in top))

        if self.verify_top_n and candidates:
            head = candidates[: self.verify_top_n]
            tail = candidates[self.verify_top_n :]
            confirmed, verify_notes = self._verify(head)
            notes.extend(verify_notes)
            for c in tail:
                c.extra["buybox_verified"] = "not checked (below cutoff)"
            candidates = confirmed + tail

        return ScanResult(
            candidates, excluded, self.client.spent - start_spend, notes
        )

    def _verify(
        self, shortlist: list[Candidate]
    ) -> tuple[list[Candidate], list[str]]:
        """Pass 3: re-price the shortlist against the real buy box.

        Returns (confirmed, notes). A row whose buy-box economics fail is
        REMOVED, not merely annotated: the free NEW proxy has been observed
        overstating a gap price by 71%, turning a projected £30.95/unit profit
        into an £18.44/unit loss. Shipping that row to the operator is the worst
        outcome this strategy can produce.
        """
        if not shortlist:
            return [], []
        try:
            verified = self.client.product(
                [c.asin for c in shortlist],
                stats_days=HISTORY_DAYS,
                buybox=True,
                max_age_s=None,
            )
        except TokenBudgetExceeded as exc:
            return shortlist, [f"buybox verification SKIPPED ({exc}); rows unconfirmed"]

        confirmed: list[Candidate] = []
        failed = 0
        disagreed = 0
        no_data = 0

        for candidate in shortlist:
            product = verified.get(candidate.asin)
            if not product:
                candidate.extra["buybox_verified"] = "not returned"
                confirmed.append(candidate)
                continue

            hist = ProductHistory.from_product(product)
            window = hist.window(HISTORY_DAYS)
            gaps = hist[csv_types.AMAZON].runs(
                is_missing, window.start, window.end, min_minutes=MIN_GAP_HOURS * 60
            )
            bb_price = hist[csv_types.BUY_BOX_SHIPPING].median_over_windows(gaps)
            proxy = candidate.extra["gap_price_p"]
            reference = candidate.current_price_p

            if not bb_price or not reference:
                # Cannot confirm and cannot disprove. Keep it, but say so.
                no_data += 1
                candidate.extra["buybox_verified"] = "no buy box data"
                confirmed.append(candidate)
                continue

            disagreement = abs(bb_price - proxy) / proxy
            bb_uplift = bb_price / reference
            required = candidate.extra["required_uplift_pct"] / 100 + 1
            candidate.extra["buybox_gap_price_p"] = bb_price
            candidate.extra["buybox_uplift_pct"] = round((bb_uplift - 1) * 100, 1)
            candidate.extra["proxy_disagreement_pct"] = round(disagreement * 100, 1)

            if bb_uplift < required:
                failed += 1
                continue  # dropped from the shortlist entirely

            if disagreement > PROXY_DISAGREEMENT_TOLERANCE:
                disagreed += 1
                candidate.extra["buybox_verified"] = "CHECK CHART -- series disagree"
            else:
                candidate.extra["buybox_verified"] = "yes"
            confirmed.append(candidate)

        notes = [
            f"buybox verification: {len(shortlist)} checked, {failed} dropped as "
            f"unprofitable on real buy box data, {disagreed} flagged for manual "
            f"chart check, {no_data} had no buy box history"
        ]
        if failed:
            notes.append(
                "NOTE: rows dropped here would have been losses. The free NEW "
                "proxy is for ranking a wide scan, not for committing money."
            )
        return confirmed, notes
