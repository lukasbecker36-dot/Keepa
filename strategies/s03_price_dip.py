"""Strategy 3 — price-dip flips.

THE TRADE
    Buy a unit NOW, while its price sits at a transient low, hold it, and relist
    at the price the market normally pays once the dip reverts. Buy price and
    reference price come from the SAME series over time, which is what makes this
    scoreable without any external cost data -- exactly as in Strategy 2, but the
    trigger is a general price dip rather than specifically an Amazon stockout.

    "Relist at average" is the whole idea, and the operator's framing of it is
    the right one: a price that has fallen far below its recent typical level,
    bought and held for the reversion. The reference the operator named is the
    90-day average; this module uses the 90-day time-weighted MEDIAN as that
    reference (the project's default summary statistic -- see series.py -- because
    the median ignores the brief extreme excursions Keepa series are full of, and
    because a mean is dragged down by the very dip we are trying to measure
    against).

THE TRAP, AND THE FILTER THAT DEFENDS AGAINST IT
    Not every price far below its average reverts. Most do not: a newer
    competitor undercuts, the line is discontinued and clearance-dumped, a review
    wave kills demand. Buying into those is buying a falling knife -- the price
    keeps going and never comes back.

    Two filters separate a flip from a knife, and both are load-bearing:

      1. RECOVERED FROM SIMILAR LOWS >= 2x IN 180 DAYS. This is the evidence
         that the price BEHAVES like a mean-reverting series rather than a
         step-down. It is the single most important filter in the strategy. One
         prior recovery is an anecdote; two is a pattern. The current dip is
         ongoing and is NOT counted -- we require prior completed cycles, which
         is what proves the pattern exists before we commit money to it.

      2. DEMAND INTACT. The current sales rank must sit close to its own 90-day
         level. If the rank collapsed alongside the price, the product is dying,
         not on sale, and the "dip" is a demand story wearing a price costume.

    A structural collapse also defeats itself against the reference over time:
    once the price has sat at its new low for 90 days, the 90-day median tracks
    down to meet it and the item no longer reads as below its average. The
    recovery filter is what catches a collapse that is still RECENT, before the
    average has caught up -- which is precisely when a naive "below average" rule
    is most dangerous.

PROXY RELIABILITY -- read before trusting a row.
    The wide scan prices everything off csv[1] NEW, which is free. NEW is the
    lowest New offer excluding shipping and can be set by a single hopeful or
    mistaken offer, so the top N are re-checked against csv[18] BUY_BOX_SHIPPING
    (the price a buyer actually pays, 3 tokens/ASIN) in pass 2. A row whose buy
    box does not confirm the dip is real and buyable NOW is dropped, not
    annotated -- the same discipline Strategy 2 arrived at the hard way.

OPERATIONAL CAVEAT, printed into every report.
    The operator is merchant-fulfilled. Winning the sale at the reverted price
    is not guaranteed against an FBA seller even once the price recovers, and the
    reversion ties up capital for the days_to_revert estimate. Both are surfaced
    on every row rather than buried.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean

from core import csv_types, fees, filters
from core.client import KeepaClient, TokenBudgetExceeded
from core.keepa_time import MINUTES_PER_DAY
from core.series import ProductHistory, Window, is_missing
from strategies.base import Candidate, Excluded, ScanResult, Strategy

# -- tunable thresholds ---------------------------------------------------
# Named constants at the top so tuning is one edit (CLAUDE.md working style).

PRICE_FLOOR_P = 1500          # £15 -- the BUY price is the dip, already low; the
                              # profit floor below is the real gate, but under
                              # ~£15 the flat handling costs rarely leave £3.
PRICE_CEILING_P = 8000        # £80 -- capital ceiling on a £1,000 float

REFERENCE_DAYS = 90           # the "90-day average" the operator named
RANGE_DAYS = 180              # the window volatility and recoveries are read over

MIN_DIP_PCT = 0.20            # current must sit >= 20% below the 90-day median.
                              # This is "far below its average" made explicit.
BOTTOM_BAND = 0.15            # ...and within the bottom 15% of the 180-day range
                              # (CLAUDE.md's "bottom 10% of range", loosened
                              # slightly). Two readings of "at a low" that must
                              # agree, so a mild sag below the average that is
                              # nowhere near the historical floor does not pass.
MIN_RANGE_WIDTH = 0.30        # (max-min)/max over 180d. No volatility, nowhere
                              # to revert to, no trade.

RECOVERY_TOLERANCE = 0.10     # "recovered" == back to >= 90% of the normal level
MIN_RECOVERIES = 2            # prior completed low->recovery cycles in 180 days

MAX_RANK_AVG = 50_000         # real ongoing demand (90-day median rank)
RANK_DRIFT_TOLERANCE = 0.30   # current rank within 30% of its 90-day level, or
                              # the dip is a demand collapse, not a price dip

MIN_DATA_COVERAGE = 0.80      # of the 180d window, or the statistics are noise
MIN_PROFIT_PER_UNIT_P = fees.MIN_PROFIT_PER_UNIT_P   # £3.00
TEST_ORDER_UNITS = 5          # units per position, for capital_required
# Opportunity must be capped by the capital that can actually chase it. Score is
# profit x units, and for a rank-8 product Keepa's monthlySold is in the
# thousands -- which produced an "est. GBP 30,690/month" on a GBP 1,000 float.
# Units are limited by what the float buys per cycle, and by how many cycles fit
# in a month.
CAPITAL_AVAILABLE_P = 100_000   # GBP 1,000 float
MAX_CYCLES_PER_MONTH = 6.0      # a floor of ~5 days per buy/hold/sell cycle
MISSING_VOLUME_UNITS = 1.0    # nominal monthly units when Keepa has no figure

VERIFY_TOP_N = 20             # pass 2 buy-box confirmations
# How far csv[1] NEW may sit from the real csv[18] buy box before a row is
# treated as unreliable rather than merely imprecise. Same noise band Strategy 2
# measured (1.6% apart on one product, 71% on another).
PROXY_DISAGREEMENT_TOLERANCE = 0.25

# Keepa's packageDimension is a VOLUME in cubic millimetres, not a longest side
# -- see the note in s02. 45 x 35 x 16 cm is the Royal Mail small-parcel max.
MAX_PARCEL_VOLUME_MM3 = 450 * 350 * 160   # 25,200,000

FINDER_PAGES = 5

# Product Finder narrows server-side; analyse() re-derives everything
# authoritatively over the free history, so a finder field that behaves
# unexpectedly costs recall, never a wrong row. Field names follow the
# <agg><window>_<TYPE>_<op> convention confirmed live for Strategy 2; verify
# deltaPercent90_NEW against ProductFinderRequest.java before relying on recall.
SELECTION = {
    # The BUY price -- current lowest New offer, banded to the workable range.
    "current_NEW_gte": PRICE_FLOOR_P,
    "current_NEW_lte": PRICE_CEILING_P,
    # Currently well below the 90-day average. This is the dip, pre-filtered
    # server-side; analyse() confirms it against the real history.
    #
    # SIGN: Keepa's deltaPercent is POSITIVE for a discount -- it measures how
    # far BELOW the average the current price sits. Verified live: `_lte: -20`
    # returned 60 of 60 products priced ABOVE their 90-day median (the exact
    # opposite of a dip, and the reason the first run scored nothing), while
    # `_gte: 20` returned 20 of 20 genuine dips out of a 2,271 population.
    "deltaPercent90_NEW_gte": int(MIN_DIP_PCT * 100),
    # Real ongoing demand.
    "avg90_SALES_gte": 1,
    "avg90_SALES_lte": MAX_RANK_AVG,
    "monthlySold_gte": 10,
    # Merchant-fulfilled physical envelope (see s02 for the volume/length split).
    "packageWeight_gte": 1,
    "packageWeight_lte": 2000,
    "packageDimension_lte": MAX_PARCEL_VOLUME_MM3,
    # Never trade against Amazon retail. If Amazon holds the buy box, the "dip"
    # is usually Amazon promoting its own stock -- and when the price reverts
    # Amazon is still there holding the buy box, so the sale is never winnable.
    # Strategy 3's first live run ranked Echo Show 5 and Echo Spot first and
    # second for exactly this reason.
    "buyBoxIsAmazon": False,
    "isHazMat": False,
    "productType": [0],
    "singleVariation": True,
    # Prefer the highest-velocity dips; volume drives the score.
    "sort": [["monthlySold", "desc"]],
}


@dataclass
class Analysis:
    """Everything measured for one ASIN. `rejected` is None if it survived."""

    asin: str
    rejected: str | None = None
    window: Window | None = None
    current_price_p: int = 0       # the BUY price -- today's dip
    reference_price_p: int = 0     # 90-day median -- the average dipped below
    revert_price_p: int = 0        # 180-day median -- what to relist at
    range_low_p: int = 0
    range_high_p: int = 0
    range_width: float = 0.0
    dip_pct: float = 0.0           # how far below the 90-day median, as a ratio
    recoveries: int = 0            # prior completed low->recovery cycles
    days_to_revert: float | None = None
    rank_now: int = 0
    rank_reference: int = 0
    expected_delta_p: int = 0      # revert price - current price
    profit_per_unit_p: int = 0
    monthly_sold: int = 0
    monthly_units: float = 0.0
    capital_limited_units: float = 0.0
    volume_is_nominal: bool = False
    score: float = 0.0

    @property
    def passed(self) -> bool:
        return self.rejected is None


def _count_recoveries(
    new, window: Window, low_ceiling: int, recovery_level: int
) -> tuple[int, float | None]:
    """Count prior low -> recovery cycles, and mean trough-to-recovery days.

    A cycle is: the price falls to at or below `low_ceiling` (enters a dip),
    then later rises to at or above `recovery_level` (returns to near normal).
    The two thresholds form a hysteresis band, so ordinary jitter around one
    level cannot manufacture phantom cycles.

    The current, ongoing dip is not counted: it has entered a low but has not
    yet recovered, so it never completes a cycle here. That is deliberate -- we
    are asking whether this price has a PROVEN habit of coming back, before we
    buy into the dip it is in now.
    """
    count = 0
    durations: list[float] = []
    state = "normal"
    trough_time: int | None = None

    start_value = new.at(window.start)
    if start_value is not None and start_value >= 0 and start_value <= low_ceiling:
        state = "low"
        trough_time = window.start

    for seg in new.segments(window.start, window.end):
        v = seg.value
        if v < 0:  # no offer recorded; do not let a gap fabricate a transition
            continue
        if state == "normal":
            if v <= low_ceiling:
                state = "low"
                trough_time = seg.start
        else:  # in a low, waiting for recovery
            if v >= recovery_level:
                count += 1
                if trough_time is not None:
                    durations.append((seg.start - trough_time) / MINUTES_PER_DAY)
                state = "normal"
                trough_time = None

    return count, (mean(durations) if durations else None)


def analyse(product: dict, *, now: int | None = None) -> Analysis:
    """Score one ASIN as a price-dip flip.

    Pure: takes a Keepa product payload, spends nothing, and can be re-run over
    cached JSON for free. All tuning happens here, against the cache.
    """
    asin = product.get("asin", "")
    a = Analysis(asin=asin)

    hist = ProductHistory.from_product(product)
    window = hist.window(RANGE_DAYS, now=now)
    a.window = window
    if window.days < REFERENCE_DAYS:
        # Too little history to establish an average to dip below, let alone two
        # prior recovery cycles.
        a.rejected = "history_too_short"
        return a

    new = hist[csv_types.NEW]
    sales = hist[csv_types.SALES]
    if not new:
        a.rejected = "no_price_history"
        return a
    if new.coverage(window.start, window.end) < MIN_DATA_COVERAGE:
        a.rejected = "sparse_history"
        return a

    current = new.current()
    if current is None or current <= 0:
        # No offer to buy right now -- there is no dip to act on.
        a.rejected = "no_current_offer"
        return a
    a.current_price_p = current

    # The average the dip is measured against, and the level to relist at.
    ref_window = hist.window(REFERENCE_DAYS, now=now)
    reference = new.weighted_median(ref_window.start, ref_window.end)
    revert = new.weighted_median(window.start, window.end)
    if not reference or not revert:
        a.rejected = "no_reference_price"
        return a
    a.reference_price_p = reference
    a.revert_price_p = revert

    a.dip_pct = 1.0 - current / reference
    if a.dip_pct < MIN_DIP_PCT:
        a.rejected = f"dip_only_{a.dip_pct * 100:.0f}pct_below_avg"
        return a

    lo = new.min_in(window.start, window.end)
    hi = new.max_in(window.start, window.end)
    if not lo or not hi or hi <= 0:
        a.rejected = "no_price_range"
        return a
    a.range_low_p = lo
    a.range_high_p = hi
    a.range_width = (hi - lo) / hi
    if a.range_width < MIN_RANGE_WIDTH:
        # A flat series has nowhere to revert to; the "dip" is noise.
        a.rejected = f"range_too_narrow_{a.range_width * 100:.0f}pct"
        return a
    if current > lo + BOTTOM_BAND * (hi - lo):
        # Below the average but not near the historical floor: not the bottom of
        # a cycle, so the reversion upside is unproven.
        a.rejected = "not_near_range_low"
        return a

    # THE load-bearing filter. See the module docstring.
    low_ceiling = int(revert * (1.0 - MIN_DIP_PCT))
    recovery_level = int(revert * (1.0 - RECOVERY_TOLERANCE))
    a.recoveries, a.days_to_revert = _count_recoveries(
        new, window, low_ceiling, recovery_level
    )
    if a.recoveries < MIN_RECOVERIES:
        # One recovery is an anecdote; zero is a falling knife. This is the main
        # defence against a structural collapse that has not yet dragged the
        # average down to meet it.
        a.rejected = f"only_{a.recoveries}_recoveries"
        return a

    # Demand intact: the rank must not have collapsed alongside the price.
    rank_ref = sales.weighted_median(ref_window.start, ref_window.end)
    rank_now = sales.current()
    if rank_ref is None or rank_ref <= 0 or rank_now is None or rank_now <= 0:
        a.rejected = "no_rank_data"
        return a
    a.rank_reference = rank_ref
    a.rank_now = rank_now
    if rank_ref > MAX_RANK_AVG:
        a.rejected = f"rank_avg_{rank_ref}"
        return a
    if (rank_now - rank_ref) / rank_ref > RANK_DRIFT_TOLERANCE:
        # Rank ballooned: this is a demand story, not a price dip. Buying it
        # means holding stock whose market is shrinking.
        a.rejected = f"demand_collapsed_rank_{rank_now}"
        return a

    referral_pct = fees.referral_pct_for(product)
    is_media = fees.is_media_product(product)
    try:
        weight = product.get("packageWeight")
        postage_p = fees.postage_for(weight if weight and weight > 0 else None, None)
    except fees.UnpostableError as exc:
        a.rejected = f"unpostable: {exc}"
        return a

    a.expected_delta_p = revert - current
    a.profit_per_unit_p = fees.profit(
        revert,
        current,
        referral_pct=referral_pct,
        is_media=is_media,
        postage_p=postage_p,
    )
    if a.profit_per_unit_p < MIN_PROFIT_PER_UNIT_P:
        a.rejected = f"profit_{a.profit_per_unit_p}p_below_floor"
        return a

    monthly_sold = product.get("monthlySold") or 0
    a.monthly_sold = monthly_sold
    if monthly_sold > 0:
        a.monthly_units = float(monthly_sold)
    else:
        # No "bought in past month" figure. Rank a nominal one unit so the row
        # still appears but sorts below anything with real volume data.
        a.volume_is_nominal = True
        a.monthly_units = MISSING_VOLUME_UNITS

    # CLAUDE.md: score by expected revert delta x 30-day sales estimate. The
    # delta is expressed as net profit per unit so fees are already in it.
    #
    # But cap the units by the capital that can actually chase them. Demand of
    # 3,000/month is irrelevant when GBP 1,000 buys 16 units a cycle -- without
    # this, high-volume products always sort to the top on demand they cannot
    # be funded to capture.
    units_per_cycle = CAPITAL_AVAILABLE_P / a.current_price_p
    cycles = MAX_CYCLES_PER_MONTH
    if a.days_to_revert and a.days_to_revert > 0:
        cycles = min(MAX_CYCLES_PER_MONTH, 30.0 / a.days_to_revert)
    a.capital_limited_units = units_per_cycle * cycles
    a.monthly_units = min(a.monthly_units, a.capital_limited_units)
    a.score = a.profit_per_unit_p * a.monthly_units
    return a


def _reason(a: Analysis) -> str:
    days = f"~{a.days_to_revert:.0f}d" if a.days_to_revert is not None else "unknown"
    return "; ".join(
        [
            f"£{a.current_price_p / 100:.2f} vs 90d avg £{a.reference_price_p / 100:.2f} "
            f"(-{a.dip_pct * 100:.0f}%)",
            f"recovered {a.recoveries}x in {a.window.days:.0f}d (revert {days})",
            f"relist £{a.revert_price_p / 100:.2f}, £{a.profit_per_unit_p / 100:.2f}/unit",
        ]
    )


def _notes(a: Analysis) -> str:
    notes = [
        "MERCHANT-FULFILLED: winning the sale at the reverted price is not "
        "guaranteed against FBA; capital is tied up until reversion.",
        f"180d range £{a.range_low_p / 100:.2f}-£{a.range_high_p / 100:.2f} "
        f"({a.range_width * 100:.0f}% wide)",
        f"rank now {a.rank_now:,} vs 90d {a.rank_reference:,}",
    ]
    if a.volume_is_nominal:
        notes.append("no monthlySold figure; volume is nominal, ranking unreliable")
    return "; ".join(notes)


def to_candidate(product: dict, a: Analysis) -> Candidate:
    tree = product.get("categoryTree") or []
    category = tree[-1].get("name", "") if tree else ""
    return Candidate(
        asin=a.asin,
        title=product.get("title") or "",
        category=category,
        current_price_p=a.current_price_p,
        score=a.score,
        strategy="s03_price_dip",
        reason=_reason(a),
        capital_required_p=a.current_price_p * TEST_ORDER_UNITS,
        est_monthly_opportunity_p=int(a.score),
        days_to_realise=a.days_to_revert,
        notes=_notes(a),
        extra={
            "reference_price_p": a.reference_price_p,
            "revert_price_p": a.revert_price_p,
            "dip_pct": round(a.dip_pct * 100, 1),
            "range_low_p": a.range_low_p,
            "range_high_p": a.range_high_p,
            "range_width_pct": round(a.range_width * 100, 1),
            "recoveries_180d": a.recoveries,
            "days_to_revert": (
                round(a.days_to_revert, 1) if a.days_to_revert is not None else None
            ),
            "expected_delta_p": a.expected_delta_p,
            "profit_per_unit_p": a.profit_per_unit_p,
            "rank_now": a.rank_now,
            "rank_reference": a.rank_reference,
            "monthly_sold": a.monthly_sold,
            "monthly_units": round(a.monthly_units, 2),
            "capital_limited_units": round(a.capital_limited_units, 2),
            "volume_is_nominal": a.volume_is_nominal,
        },
    )


class PriceDipStrategy(Strategy):
    name = "s03_price_dip"
    description = "Buy a transient price low, hold, relist at the reverted average"

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
            "The trade: buy today's low, hold, relist at the reverted average.",
            "'Recovered >= 2x' is the falling-knife defence -- a dip that has "
            "never come back is not scored.",
            "Dip prices use csv[1] NEW (free). Top rows re-checked against "
            "csv[18] buy box -- see buybox_verified in the CSV.",
        ]
        excluded: list[Excluded] = []
        candidates: list[Candidate] = []
        start_spend = self.client.spent

        try:
            asins = self.client.product_finder_pages(self.selection, pages=self.pages)
            notes.append(f"finder returned {len(asins)} ASINs")
            products = self.client.product(asins, stats_days=RANGE_DAYS)
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
        """Pass 2: confirm the dip against the real buy box.

        The free NEW proxy can be set by one hopeful or mistaken offer. Two ways
        a row can fail here, and both REMOVE it rather than flag it:

          * the current buy box does not confirm a dip is available to buy NOW
            (NEW dipped but the buy box did not -- e.g. only a lone FBM offer
            dropped), so there is nothing to acquire at the low price;
          * the historical buy box does not support the revert target, so the
            price we expect to relist at was never really paid.
        """
        if not shortlist:
            return [], []
        try:
            verified = self.client.product(
                [c.asin for c in shortlist],
                stats_days=RANGE_DAYS,
                buybox=True,
                max_age_s=None,
            )
        except TokenBudgetExceeded as exc:
            return shortlist, [f"buybox verification SKIPPED ({exc}); rows unconfirmed"]

        confirmed: list[Candidate] = []
        no_dip = 0
        no_support = 0
        disagreed = 0
        no_data = 0

        for candidate in shortlist:
            product = verified.get(candidate.asin)
            if not product:
                candidate.extra["buybox_verified"] = "not returned"
                confirmed.append(candidate)
                continue

            hist = ProductHistory.from_product(product)
            window = hist.window(RANGE_DAYS)
            bb = hist[csv_types.BUY_BOX_SHIPPING]
            bb_now = bb.current()
            bb_typical = bb.weighted_median(window.start, window.end)
            reference = candidate.extra["reference_price_p"]
            revert = candidate.extra["revert_price_p"]

            if not bb_now or bb_now < 0 or not bb_typical:
                no_data += 1
                candidate.extra["buybox_verified"] = "no buy box data"
                confirmed.append(candidate)
                continue

            candidate.extra["buybox_now_p"] = bb_now
            candidate.extra["buybox_typical_p"] = bb_typical

            # Is a dip actually buyable on the buy box right now?
            if bb_now > int(reference * (1.0 - MIN_DIP_PCT)):
                no_dip += 1
                continue
            # Does the buy box history support relisting near the revert target?
            if bb_typical < int(revert * (1.0 - PROXY_DISAGREEMENT_TOLERANCE)):
                no_support += 1
                continue

            disagreement = abs(bb_typical - revert) / revert
            candidate.extra["proxy_disagreement_pct"] = round(disagreement * 100, 1)
            if disagreement > PROXY_DISAGREEMENT_TOLERANCE:
                disagreed += 1
                candidate.extra["buybox_verified"] = "CHECK CHART -- series disagree"
            else:
                candidate.extra["buybox_verified"] = "yes"
            confirmed.append(candidate)

        notes = [
            f"buybox verification: {len(shortlist)} checked, {no_dip} dropped "
            f"(no dip on the real buy box), {no_support} dropped (buy box never "
            f"supported the revert price), {disagreed} flagged for a chart check, "
            f"{no_data} had no buy box history"
        ]
        if no_dip or no_support:
            notes.append(
                "NOTE: rows dropped here priced off a lone NEW offer with no "
                "market behind it. The free proxy ranks a wide scan; it does not "
                "commit money."
            )
        return confirmed, notes
