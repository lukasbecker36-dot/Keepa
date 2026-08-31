"""One-off live check of the decoder against real Keepa data. Costs ~1-11 tokens.

The unit tests prove the maths is self-consistent. They cannot prove the two
assumptions that would silently corrupt everything downstream:

  1. The Keepa epoch is 2011-01-01Z, so decoded timestamps land on real dates.
  2. csv[18] really is triplets and csv[0] really is pairs, on live payloads.

Both are checked here against data Keepa itself computed: our own Amazon-OOS
percentage is compared with `stats.outOfStockPercentage90`, which Keepa derives
server-side. If those agree, the window maths and the epoch are both right.

The response is saved to tests/fixtures/ so development never needs to refetch --
at 5 tokens/min that matters more than it sounds.

    python -m scripts.verify_live --asin B0XXXXXXXX
    python -m scripts.verify_live --discover      # +10 tokens, picks an ASIN
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import config, csv_types  # noqa: E402
from core.cache import Cache  # noqa: E402
from core.client import KeepaClient, RESERVE_INTERACTIVE  # noqa: E402
from core.keepa_time import (  # noqa: E402
    MINUTES_PER_DAY,
    days_ago_keepa,
    format_london,
    now_keepa,
)
from core.series import ProductHistory, is_missing  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

# Prefer a product with real history; young ASINs make every percentage noisy.
TRACKED_AT_LEAST_A_YEAR = days_ago_keepa(365)

# A cheap, broad query used only to find one live ASIN to inspect.
DISCOVER = {
    "current_BUY_BOX_SHIPPING_gte": 2000,
    "current_BUY_BOX_SHIPPING_lte": 6000,
    "current_SALES_gte": 1,
    "current_SALES_lte": 30_000,
    # Amazon must actually have held the buy box: without this, the query matches
    # products Amazon has NEVER stocked (100% OOS, one data point, no buy box
    # history), which exercise none of the decoding this script exists to check.
    "buyBoxStatsAmazon180_gte": 60,
    "outOfStockPercentage90_gte": 20,
    "outOfStockPercentage90_lte": 85,
    "trackingSince_lte": TRACKED_AT_LEAST_A_YEAR,
    "productType": [0],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asin", help="ASIN to inspect (any Amazon UK product)")
    ap.add_argument(
        "--discover", action="store_true", help="spend 10 extra tokens to pick one"
    )
    args = ap.parse_args()

    if not args.asin and not args.discover:
        ap.error("pass --asin B0XXXXXXXX, or --discover to spend 10 tokens finding one")

    cache = Cache()
    try:
        client = KeepaClient(
            cache=cache, strategy="verify", reserve=RESERVE_INTERACTIVE, max_tokens=30
        )
    except config.MissingApiKey as exc:
        # Setup problem, not a crash -- show the instructions, not a traceback.
        print(exc, file=sys.stderr)
        return 2
    print(f"start: {client.summary()}")

    asin = args.asin
    if not asin:
        found = client.product_finder(DISCOVER)
        if not found:
            print("finder returned nothing; pass --asin explicitly")
            return 1
        asin = found[0]
        print(f"discovered {asin}")

    # max_age_s=None forces a live fetch; we are here to check live decoding.
    products = client.product([asin], stats_days=180, max_age_s=None)
    product = products.get(asin)
    if not product:
        print(f"no product returned for {asin}")
        return 1

    FIXTURES.mkdir(parents=True, exist_ok=True)
    fixture = FIXTURES / f"product_{asin}.json"
    fixture.write_text(json.dumps(product, indent=1), encoding="utf-8")

    hist = ProductHistory.from_product(product)
    now = now_keepa()
    # Clamped to trackingSince -- Keepa's own percentages are computed over the
    # tracked period, and comparing against a raw 90 days is not like for like.
    win90 = hist.window(90, now=now)
    t0_90 = win90.start

    print(f"\n{asin}  {product.get('title', '')[:70]}")
    print(f"brand={product.get('brand')}  monthlySold={product.get('monthlySold')}")
    tracked = hist.tracked_days(now=now)
    print(
        f"trackedFor={tracked:.0f}d" if tracked else "trackedFor=unknown",
        f" window={win90.days:.1f}d (clamped to tracking)",
    )
    print(f"referralFeePercentage={product.get('referralFeePercentage')}")
    print(f"fixture -> {fixture.relative_to(Path.cwd()) if fixture.is_relative_to(Path.cwd()) else fixture}")

    print("\n-- decoded series --")
    for idx in (
        csv_types.AMAZON,
        csv_types.BUY_BOX_SHIPPING,
        csv_types.SALES,
        csv_types.NEW_FBA,
        csv_types.COUNT_NEW_FBA,
    ):
        s = hist[idx]
        if not s:
            print(f"  {s.name:<20} absent")
            continue
        stride = csv_types.BY_INDEX[idx].stride
        print(
            f"  {s.name:<20} stride={stride} points={len(s):<5} "
            f"last={format_london(s.last_time)} value={s.current()}"
        )

    ok = True

    # -- check 1: the epoch --------------------------------------------------
    amazon = hist[csv_types.AMAZON]
    anchor = amazon.last_time or hist[csv_types.SALES].last_time
    print("\n-- check 1: epoch --")
    if anchor is None:
        print("  SKIP  no timestamped series on this ASIN")
    else:
        age_days = (now - anchor) / MINUTES_PER_DAY
        print(f"  newest point is {age_days:.1f} days old ({format_london(anchor)})")
        if -1 <= age_days <= 400:
            print("  PASS  timestamps land in a plausible recent range")
        else:
            ok = False
            print("  FAIL  epoch is wrong -- KEEPA_EPOCH_MINUTES is off")

    # -- check 2: our OOS maths vs Keepa's own ------------------------------
    print("\n-- check 2: Amazon OOS %, ours vs Keepa's stats --")
    stats = product.get("stats") or {}
    keepa_oos = (stats.get("outOfStockPercentage90") or [None])[csv_types.AMAZON]
    if not amazon:
        print("  SKIP  no Amazon series (Amazon may never have sold this ASIN)")
    elif keepa_oos is None or keepa_oos < 0:
        print(f"  SKIP  Keepa reports no outOfStockPercentage90 (got {keepa_oos})")
    else:
        ours = amazon.missing_fraction(t0_90, now) * 100
        delta = abs(ours - keepa_oos)
        print(f"  ours={ours:.1f}%   keepa={keepa_oos}%   delta={delta:.1f}pp")
        if delta <= 5:
            print("  PASS  window maths and epoch agree with Keepa")
        else:
            ok = False
            print("  FAIL  disagreement means the window maths or epoch is wrong")

    # -- check 3: stride sanity on the triplet series -----------------------
    print("\n-- check 3: csv[18] triplet decoding --")
    bb = hist[csv_types.BUY_BOX_SHIPPING]
    if not bb:
        print("  SKIP  no buy box series")
    else:
        med = bb.weighted_median(t0_90, now)
        cur = product.get("stats", {}).get("buyBoxPrice")
        free_med = new.weighted_median(t0_90, now) if new else None
        print(f"  90d median (incl. shipping) = {med}p   stats.buyBoxPrice = {cur}p")
        if free_med and med:
            print(
                f"  free proxy csv[1] NEW = {free_med}p "
                f"({abs(free_med - med) / med * 100:.1f}% from buy box)"
            )
        # Misreading triplets as pairs interleaves shipping into the value
        # stream, which shows up as wildly implausible prices.
        if med is not None and 50 <= med <= 500_000:
            print("  PASS  prices are in a plausible pence range")
        else:
            ok = False
            print("  FAIL  implausible prices -- stride is being read wrongly")

    # -- gap summary, the Strategy 2 shape ----------------------------------
    if amazon:
        gaps = amazon.runs(is_missing, t0_90, now, min_minutes=24 * 60)
        print(f"\n-- Amazon stock gaps >24h in last 90d: {len(gaps)} --")
        for g in gaps[:5]:
            price = bb.weighted_median(g.start, g.end) if bb else None
            print(
                f"  {format_london(g.start)} -> {format_london(g.end)}  "
                f"{g.days:.1f}d  buybox median={price}p"
            )
        if gaps and bb:
            ref = amazon.weighted_median(t0_90, now)
            gap_price = bb.median_over_windows(gaps)
            if ref and gap_price:
                print(
                    f"  reference={ref}p  gap={gap_price}p  "
                    f"uplift={(gap_price / ref - 1) * 100:.0f}%"
                )

    print(f"\nend: {client.summary()}")
    print("RESULT:", "all checks passed" if ok else "CHECKS FAILED -- do not build on this")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
