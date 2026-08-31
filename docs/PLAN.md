# Implementation plan — Keepa UK sourcing toolkit

Scope: overall architecture, then Strategy 2 (Amazon OOS arbitrage) and Strategy 1
(private-label niche discovery) in build-ready detail. Strategies 3–9 are
sequenced at the end but not specified.

Verified against the Keepa API structs on 2026-08-31 (`keepacom/api_backend`:
`ProductFinderRequest.java`, `Product.java`, `Stats.java`) — field names below are
real, not assumed.

---

## 0. Environment state

- Python 3.11.9 present. `keepa` package **not installed**.
- `KEEPA_API_KEY` **not set** (checked bash + Windows user scope). Set before anything runs.
- `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` not set — not needed for Strategies 1 and 2.
- Repo is currently just `CLAUDE.md`. Not a git repo yet.

---

## 1. Repo layout

```
keepa/
  core/
    client.py        # Keepa wrapper: token governor, retry, batching
    cache.py         # SQLite read-through cache with per-table freshness windows
    series.py        # Keepa csv[] decode + time-window arithmetic
    fees.py          # single source of truth for all cost/fee maths (pence)
    filters.py       # negative filters, applied uniformly, records reason
    scoring.py       # normalisation helpers so scores are comparable
    report.py        # CSV + summary.md + excluded.csv writers
  strategies/
    base.py          # Strategy ABC: scan() -> list[Candidate]
    s02_amazon_oos.py
    s01_niche_discovery.py
  data/
    keepa.db
    gated_brands.txt
    categories_uk.json
    retail_watchlist.csv
  output/YYYY-MM-DD/
  run.py             # CLI: run.py --strategy s02 --dry-run --max-tokens 800
  tests/
```

`Candidate` is a frozen dataclass matching the CSV columns in CLAUDE.md, so
`report.py` never needs to know which strategy produced a row.

---

## 2. Core modules — build these first

### 2.1 `core/client.py` — token governor

The most important piece of discipline in the project. Keepa refills at a fixed
rate per minute against one bucket shared by every process using the key.

Bucket: **300 cap, 5/min refill** (confirmed). See §5 — this governs everything.

- Read `tokensLeft` and `refillRate` off every response; persist to SQLite.
- Hard floor: `KEEPA_TOKEN_RESERVE` — 50 for the nightly job, 150 interactive.
  Below it, block on refill; abort and write a partial report only if the wait
  would overrun the run window.
- Per-run ceiling: `--max-tokens`. Every call debits a projected cost *before*
  issuing, so a runaway loop cannot drain the bucket.
- Batch size is capped by the bucket, not just by Keepa's 100-ASIN limit: a
  100-ASIN batch is 100 tokens and 20 minutes of refill. Size batches to what the
  bucket currently holds.
- Log tokens per strategy per run into a `token_ledger` table, so it is obvious
  which scan is expensive.

**Measured costs** — taken from the `token_ledger` after live calls on
2026-08-31, not from the docs. Two of the documented figures are wrong:

| Call | Documented | **Measured** |
|---|---|---|
| Product Finder query | 10 | **11** / page |
| Product + history + `stats` | 1 | **1** / ASIN |
| `stats` parameter | — | **free** (confirmed) |
| `buybox=True` | 5 | **+2** → 3 / ASIN total |
| `offers` | 6 per 10 offers | not measured |
| Best Sellers | 50 / category | not measured |

**The `extraData` finding.** A plain `history=1&stats=180` fetch does *not*
return every csv series. On a live product the response carried indices
[0, 1, 2, 3, 4, 11, 13, 33, 34, 35] — and every absent series was one flagged
`isExtraData` in the CsvType table, **including csv[18] BUY_BOX_SHIPPING**.
Setting `buybox=1` makes csv[18] appear (1129 points on the same ASIN), and
`stats.buyBoxPrice` changes from the -2 "not requested" sentinel to a real price.

So the original plan's assumption — that csv[18] arrives free with history — was
wrong. See §3.2 for what replaces it.

### 2.2 `core/series.py` — the csv[] decoder

Keepa returns each metric as a flat `[keepa_minutes, value, keepa_minutes, value, ...]`
step function. Confirmed indices we use:

| idx | name | use |
|---|---|---|
| 0 | `AMAZON` | Amazon's own price. **`-1` = Amazon out of stock.** |
| 3 | `SALES` | sales rank |
| 10 | `NEW_FBA` | lowest 3rd-party FBA offer |
| 11 | `COUNT_NEW` | new offer count |
| 17 | `COUNT_REVIEWS` | review count history |
| 18 | `BUY_BOX_SHIPPING` | buy box price incl. shipping |
| 34 | `COUNT_NEW_FBA` | FBA seller count |
| 35 | `COUNT_NEW_FBM` | FBM seller count |

**Stride is not uniform, and this is a live trap.** The `CsvType` enum's
constructor is `(index, isPrice, isDealRelevant, isWithShipping, isExtraData)`,
and `BUY_BOX_SHIPPING(18, true, false, true, true)` has `isWithShipping = true`.
Series flagged that way are `[time, price, shipping]` **triplets**; everything
else is `[time, value]` **pairs**. `csv[0]` is pairs, `csv[18]` is triplets.

Decoding 18 with a stride of 2 reads shipping costs as timestamps and prices as
shipping — it raises nothing and produces numbers that look like prices. Since
csv[18] is the series Strategy 2's entire delta calculation rests on, the stride
table is transcribed verbatim into `core/csv_types.py` and the decoder always
takes its stride from there. Combining price + shipping gives the landed total
the buyer actually pays, which is the right basis for comparison.

Prices are in the locale's minor unit — pence for domain 2. Keepa time is minutes
since a fixed epoch; assert the conversion against a known ASIN in tests rather
than trusting a constant copied from a blog post.

API surface:

```python
Series.at(t)                  -> value | None
Series.runs(pred)             -> [(t_start, t_end)]   # contiguous windows
Series.median_in(t0, t1)      -> int | None            # TIME-weighted
Series.resample_daily(t0, t1) -> list[int | None]
```

Time-weighted medians matter. Keepa emits a point only on *change*, so a price
held for 40 days and a price held for 40 minutes are each one sample. A
sample-weighted average will lie to you, consistently and invisibly. This is the
subtlest bug in the whole project — unit test it with synthetic step functions
before anything depends on it.

### 2.3 `core/fees.py`

One module, used by every strategy, all integers in pence.

```python
def net_proceeds(sell_price_p, asin_meta, channel="amazon_mf") -> Breakdown
def landed_cost(unit_cost_p, shipping_in_p, ...)               -> int
```

- Referral %: use `product.referralFeePercentage` **per ASIN** rather than a flat
  15%. Keepa returns it; the brief's flat assumption is a needless source of
  error. Fall back to a category table when null.
- Media closing fee £0.75 where binding/productType indicates media.
- Outbound: Royal Mail 2nd class bands by weight and longest side. Only the
  £2.95 small-parcel figure comes from the brief; the large-letter and
  medium-parcel entries are flagged unverified in the code and should be checked
  against the current rate card before being relied on. Unknown weight falls back
  to small parcel rather than guessing cheap — understating postage silently
  inflates every margin in the shortlist.
- Amazon's Digital Services Fee is present but set to 0: it applies in some
  marketplaces as a percentage of the referral fee, and baking in a number that
  may not apply to this account would be worse than leaving it off. Verify.
**VAT: not registered.** Settled, and it resolves as follows:

- **Seller fees carry 20% VAT you cannot reclaim.** Amazon charges VAT on fees to
  non-VAT-registered UK sellers, so a 15% referral fee actually costs 18% of the
  sale price. Apply the multiplier inside `fees.py` — never let a headline
  referral rate reach a strategy module ungrossed.
- **Purchase cost is the gross shelf price.** No input VAT reclaim, so the £30 you
  pay Amazon is £30 of cost, not £25 plus recoverable VAT. For Strategy 2 this is
  the whole reason the 40% gap-delta threshold has to hold.
- **No output VAT on sales** while under the £90k registration threshold — the
  sale price is yours gross. This is the one part that favours you, and it is why
  thin-margin flips work now that would not work post-registration.
- Royal Mail universal services are VAT-exempt, so £2.95 is £2.95. Packaging is
  bought gross.

Implemented in `core/fees.py`. `FeeConfig` carries the whole environment and
`DEFAULT.registered()` returns the post-threshold version, so the effect of
registering can be modelled before crossing £90k rather than discovered after.

Also implemented there, and used by Strategy 2 instead of a flat threshold:

```python
required_sell_price(unit_cost_p, target_profit_p)   # exact, not approximate
required_uplift(unit_cost_p, target_profit_p)       # replaces the flat 1.40
breakeven_sell_price(unit_cost_p)
```

`required_sell_price` solves analytically then walks to the exact pence boundary
under the same `profit()` the strategies call — it is a filter threshold, so an
off-by-one pence silently includes or drops candidates.

Keep the flag rather than hardcoding the unregistered case. Crossing £90k in
rolling turnover inverts every one of the bullets above, and you want that to be
a one-line change, not a rewrite. Worth logging cumulative turnover somewhere so
the threshold does not arrive unnoticed.

### 2.4 `core/filters.py`

Every negative filter from CLAUDE.md, each returning a named reason string.
Excluded candidates go to `excluded.csv` tagged with the filter that caught them.

Push what you can into the Finder query itself (free, server-side): `isHazMat`,
`batteriesRequired`, `batteriesIncluded`, `isAdultProduct`, `packageWeight_lte`,
`packageDimension_lte`. Do the rest (gated brands, IP-hot keywords,
trademark-unknown) locally after fetch.

---

## 3. Strategy 2 — Amazon out-of-stock arbitrage

**The trade:** buy the unit *from Amazon* while Amazon has it in stock and cheap;
hold; sell when Amazon goes out of stock and the buy box price jumps. Source price
and reference price are the same number, which is what makes this scoreable
without any external cost data.

### 3.1 Pass 1 — Product Finder (10 tokens/query)

Every one of these is a real `ProductFinderRequest` field:

```python
SELECTION = {
    "buyBoxStatsAmazon180_gte":   60,      # Amazon held BB >=60% of 180d
    "outOfStockPercentage90_gte": 20,      # unqualified field == Amazon OOS %
    "outOfStockPercentage90_lte": 85,      # >85% = abandoned, not cyclical
    "current_BUY_BOX_SHIPPING_gte": 1500,  # >= GBP 15 floor from brief
    "current_BUY_BOX_SHIPPING_lte": 8000,  # capital ceiling on a GBP 1000 float
    "avg90_SALES_gte": 1,
    "avg90_SALES_lte": 50000,
    "offerCountFBA_lte": 10,
    "monthlySold_gte": 10,
    "packageWeight_lte": 2000,             # grams  (verify unit in test)
    "packageDimension_lte": 450,           # mm     (verify unit in test)
    "isHazMat": False,
    "productType": [0],
    "sort": [["outOfStockPercentage90", "desc"]],
    "perPage": 50, "page": 0,
}
```

`buyBoxStatsAmazon180_gte` and `outOfStockPercentage90_gte` express the brief's
first two filters exactly, server-side, for 10 tokens. That is the entire reason
to lead with the Finder rather than fetching and filtering locally.

5 pages → ~250 ASINs → 50 tokens.

### 3.2 Pass 2 — history reconstruction (1 token/ASIN), and the price basis

**csv[18] is not free**, so the wide scan cannot use it (§2.1). Measured on live
data for the same ASIN over the same 90 days:

| series | 90-day median | cost |
|---|---|---|
| csv[18] `BUY_BOX_SHIPPING` | 3849p | +2 tokens/ASIN |
| csv[1] `NEW` | 3787p | free |

That single observation looked like a safe substitution. **It does not
generalise.** On the first candidate to survive every other filter, the two
series were **71% apart**, and in the dangerous direction: NEW implied a +141%
gap uplift where the buy box showed −31% — a £18.44/unit loss dressed up as a
£30.95/unit profit.

The mechanism: csv[1] NEW is the lowest New offer *excluding shipping*, and it
goes erratic exactly when Amazon is absent and few offers remain; csv[18] is
sparse and can carry a stale value straight through a gap. On that ASIN, NEW
spiked to £84.38 for 1.7 of a 2.6-day gap while the buy box series sat unchanged
at £43.09.

So the proxy is fit for **ranking a wide scan cheaply**. It is not fit for
committing money, and pass 3 is not a formality.

### Sales validation — price PAID, not price ASKED

The deeper fix, and it costs nothing. A sales-rank drop is a unit sold, so the
price in force at that instant is a *transaction*. Taking the median over sale
events rather than over time separates a real market from one seller's hopeful
ask:

| Crocs B0C4G674CY | price |
|---|---|
| Asked — time-weighted `NEW` median | £84.38 |
| **Paid — median at sales-rank drops** | **£28.63** |
| Real buy box (3 tokens) | £24.14 |

The two stock gaps showing £84.38 contained **zero rank drops**. Nobody bought at
that price. Sales validation lands within 19% of the paid truth where the
time-weighted ask was 250% out — and it needs no extra tokens, because csv[3]
SALES is already in the free payload.

`core.series.price_at_sales()` implements it; Strategy 2 uses it as the primary
price basis and rejects any ASIN with fewer than `MIN_SALE_EVENTS_IN_GAPS = 3`
observed sales across its gaps. Both figures go into the CSV, so a divergence
between ask and paid is visible without re-running anything.

So Strategy 2 runs two-tier:

- **Wide scan** on free series only: csv[0] AMAZON for the gaps and the reference
  price, csv[1] NEW for the gap sell price, csv[3] SALES, csv[34] COUNT_NEW_FBA.
  1 token/ASIN.
- **Shortlist verification** with `buybox=1` on the top ~20 only. 3 tokens/ASIN,
  ~60 tokens. A row that fails here is **removed from the shortlist**, not
  annotated and left in place. Rows where the two series disagree by more than
  25% are kept but flagged `CHECK CHART`.

Cheap where it is broad, exact where it matters.

`client.query(asins, domain=2, history=True, stats=180)` in batches of 100.

Per ASIN, from `csv[0]`:

1. **Gap detection.** `runs(value == -1)` over 180d. Discard gaps shorter than
   `MIN_GAP_HOURS = 24` — those are restock blips, not sellable windows.
2. **Reference price.** Time-weighted median of `csv[0]` over the *in-stock*
   periods in the same 180d. This is your buy price.
3. **Gap price.** Per gap, time-weighted median of `csv[18]` inside the gap.
4. **Delta test — price-scaled, not flat.**
   `gap_price >= fees.required_uplift(reference_price, MIN_PROFIT_PER_UNIT_P)
   * reference_price`.

   Postage and packaging are flat (£3.25 on every unit) while fees are
   proportional, so the uplift a flip needs is a function of price. Measured
   from the implemented model at a 15% referral:

   | Amazon price | breakeven uplift | uplift for £3/unit | flat 40% yields |
   |---|---|---|---|
   | £15 | 48% | 73% | **−£1.03** |
   | £22 | 40% | 57% | +£0.01 |
   | £30 | 35% | 47% | +£1.19 |
   | £50 | 30% | 37% | +£4.15 |
   | £80 | 27% | 31% | +£8.59 |

   A flat 40% rule is a guaranteed **loss** below £22 and progressively slack
   above £45, where it discards workable candidates. `fees.required_uplift()`
   replaces the constant.

   Consequence for the finder query: raise `current_BUY_BOX_SHIPPING_gte` from
   the brief's £15 to **£2500 (£25)**. Below that, clearing £3/unit needs a 57%+
   gap uplift, which Amazon stock gaps rarely produce — those rows would be
   noise even when they pass every other filter.
5. **Demand-during-gap test.** Median `csv[3]` inside gaps ≤ 50,000. Guards the
   classic trap: price "spikes" to £90 during a gap because nothing is selling
   at £90.
6. **Competition-during-gap.** Median `csv[34]` (`COUNT_NEW_FBA`) inside gaps.
7. **Cadence.** gaps/90d and mean gap length → `expected_gap_days_per_month`.

### 3.3 What we are actually tracking: Amazon's own stock state

The competitor that matters is Amazon's own inventory, not the buy box as such.
When Amazon is in stock on an ASIN it is effectively unbeatable — it holds the
default offer and prices at will. When Amazon goes out, that constraint lifts and
the price the market clears at rises. The trade is timed against Amazon's stock
state, and buy box ownership is downstream of it.

This is exactly what `csv[0] == -1` encodes, so §3.2 already measures the right
thing. Two consequences for the design:

- **`COUNT_NEW_FBA` during gaps is a ranking signal, not a gate.** With Amazon out
  and no FBA sellers present, an MF offer is the only offer and sells at the
  elevated price. With three FBA sellers already in the gap, you are a fourth
  offer at a worse fulfilment tier and the realised price will sit below the
  median gap price. So: score it, surface it, don't exclude on it.

```
gap_competition = median(COUNT_NEW_FBA during gaps)     # 0 = clear run
price_realism   = 1.0 if gap_competition == 0 else 0.6  # haircut on expected sell price
```

- **Amazon's restock is the exit deadline, not the buy box.** `avg_gap_days` is
  how long your selling window stays open. A 4-day median gap on a slow ASIN
  means the window closes before an MF dispatch cycle completes; a 30-day gap is
  comfortable. Rank by gap length as well as delta, and treat anything under
  `MIN_SELLABLE_GAP_DAYS = 7` as noise for a merchant-fulfilled operator.

Keep every row in the CSV with both columns visible — the FBA-contested ones are
still useful for calibrating how the model behaves against reality.

### 3.4 Scoring

```
profit_per_unit     = fees.net_proceeds(gap_price) - fees.landed_cost(reference_price)
gap_units_per_month = monthlySold * (expected_gap_days_per_month / 30)
score               = profit_per_unit * gap_units_per_month
```

`monthlySold` is a real field on the product object (with `monthlySoldHistory`) —
Amazon's own "bought in past month" figure, surfaced by Keepa. This is a large
upgrade on the rank→units guesswork the brief assumes, and should replace the
sales-estimate curve wherever it is populated. It is still whole-ASIN and still
coarse: ranking input, never a revenue forecast.

Extra columns: `capital_required = reference_price * 5`, `avg_gap_days`,
`gaps_per_90d`, `avg_price_delta_pct`, `mf_reachable`, `days_to_realise` (mean
days from restock to next gap onset — how long capital sits idle).

### 3.5 Two operational caveats to print in the output header

- **Amazon retail receipts are not valid invoices for brand ungating.** This
  strategy sources from Amazon itself, so it only works on ASINs you can already
  list. The gated-brand filter is load-bearing here, not advisory.
- Gaps are inferred from 180 days of history. A supplier who has permanently
  exited looks identical to a cyclical stockout until you look at the chart.
  `keepa_url` stays the most important column.

Token cost: ~300/night.

---

## 4. Strategy 1 — private-label niche discovery

### 4.1 A sharper "beatable" signal than review count

The brief uses review count under 200 as the proxy for weak defence. Keepa
exposes listing-quality fields directly, and they proxy "I can outrank this with
a better listing" far better:

`hasAPlus`, `hasAPlusFromManufacturer`, `hasMainVideo`, `videoCount`,
`imageCount`, `brandStoreName`, `variationCount`, `returnRate`.

A listing with no A+ content, no video, five images and no brand store is one you
can beat on presentation alone. A listing with 180 reviews *and* full A+ content
*and* a brand store is a real operator who will defend their position. Review
count alone cannot tell those two apart. Use both signals.

### 4.2 Finder query

```python
SELECTION = {
    "current_BUY_BOX_SHIPPING_gte": 2500,   # GBP 25
    "current_BUY_BOX_SHIPPING_lte": 6000,   # GBP 60
    "current_SALES_gte": 1,
    "current_SALES_lte": 30000,
    "current_COUNT_REVIEWS_lte": 200,
    "itemWeight_lte": 500,                  # grams
    "packageWeight_lte": 700,
    "packageDimension_lte": 450,
    "buyBoxIsAmazon": False,
    "isHazMat": False,
    "batteriesRequired": False,
    "batteriesIncluded": False,
    "isAdultProduct": False,
    "isMerchOnDemand": False,
    "singleVariation": True,
    "salesRankDrops90_gte": 30,             # proven repeat sales, not one spike
    "monthlySold_gte": 30,
    "hasAPlus": False,                      # weak-defence signal
    "hasMainVideo": False,
    "imageCount_lte": 5,
    "productType": [0],
    "categories_include": [<one leaf node id per query>],
    "perPage": 50, "page": 0,
}
```

One query per target leaf category — running unscoped returns a soup you cannot
reason about. Needs `data/categories_uk.json`; build it once from Keepa's category
endpoint and commit it.

`salesRankDrops90_gte` is the honest demand check: each drop is a sale event, so
30+ over 90 days means real recurring movement rather than one lucky week.

### 4.3 Post-pass (1 token/ASIN)

- **Rank stability:** `abs(stats.avg90[3] - stats.current[3]) / stats.avg90[3] <= 0.30`.
  Not expressible in the Finder, so it has to happen here.
- **Peer set:** group returned ASINs by `salesRankReference` and price decile;
  "avg review count of top 3" = mean `COUNT_REVIEWS` of the three best-ranked
  peers in that group. Makes the brief's scoring term computable with no extra
  API spend, and it is a better comparison than a global average.
- Local negative filters: gated brands, IP-hot keywords, trademark-unknown.

### 4.4 Scoring — normalise the terms

The brief's `rank_stability × (200 − avg_reviews_top3) × (60 − price)` collapses
to zero at the boundaries and goes negative past them, so a £61 product would
score worse than worthless. Keep the shape, normalise each factor to 0–1:

```
stability = clamp(1 - abs(rank_now - rank_avg90) / rank_avg90, 0, 1)
weakness  = clamp((200 - peer_avg_reviews) / 200, 0, 1)
            * listing_quality_penalty      # 1.0 if no A+/video, 0.4 if either present
headroom  = clamp((6000 - price_p) / 3500, 0, 1)
demand    = log1p(monthlySold) / log1p(500)
score     = 100 * stability * weakness * headroom * demand
```

Scores are ordinal within a single scan only — state that in `summary.md`.

Output adds an Alibaba **search URL** built from the product's category keywords.
Search link only, per the brief: no sourcing, no scraping.

Token cost: **2 categories per night, 2 pages each** = 40 tokens, plus ~200 ASINs
= ~240/night. Categories rotate on a 3-night cycle rather than all running each
night — see §5. Persist the rotation cursor in SQLite so a missed night resumes
where it left off instead of restarting the cycle.

---

## 5. Token budget — the binding constraint

**Confirmed: 300 token bucket cap, refilling at 5/min.** This is tighter than the
brief's framing and it is the single hardest constraint on the whole project.
Two separate limits, and the first one is easy to miss:

- **Burst ceiling: 300 tokens.** You can never spend more than 300 in one go, no
  matter how long you have been idle. The bucket does not accumulate past its cap.
- **Sustained throughput: 300 tokens/hour.** 5/min is the real budget. The
  theoretical 7,200/day only materialises if something is drawing tokens around
  the clock, which means a scan is not a burst of work — it is a *paced* job that
  spends most of its wall-clock time blocked on refill.

A 100-ASIN batch costs 100 tokens and takes 20 minutes to earn back. Plan in
hours, not seconds.

### Revised nightly shape

| step | tokens | paced time |
|---|---|---|
| S2 finder, 5 pages @ 11 | 55 | 11 min |
| S2 history, ~250 ASINs @ 1 | 250 | 50 min |
| S2 shortlist verify, 20 @ 3 (buybox) | 60 | 12 min |
| S1 finder, 2 categories × 2 pages @ 11 | 44 | 9 min |
| S1 history, ~200 ASINs @ 1 | 200 | 40 min |
| **Total** | **~610** | **~2h** |

That fits comfortably in an overnight window with headroom for retries.

**Strategy 1 rotates rather than running whole.** Two leaf categories per night,
full cycle every 3 nights. Niches do not shift on a 24-hour timescale, so nightly
full coverage buys nothing and costs everything. Strategy 2 runs nightly — stock
gaps *are* time-sensitive, and it is the cheaper of the two.

### Three consequences worth designing around

1. **The client must block, not fail.** `wait_for_tokens` between batches, with
   the projected cost checked before each call. A job that aborts at 40% because
   it hit an empty bucket has wasted the tokens it already spent.
2. **Never develop against the live API.** Record real responses to JSON fixtures
   in `tests/fixtures/` on the first successful call, then iterate offline. At
   5 tokens/min, a careless debug loop costs you the night's scan. This matters
   far more at 300/hr than it would at a higher tier.
3. **The cached-blob design in §6 stops being an optimisation and becomes the
   main development tool.** Re-scoring yesterday's stored JSON is free and
   instant; re-fetching is a two-hour wait. Every threshold you tune should be
   tuned against cache.

Reserve floors: nightly job `KEEPA_TOKEN_RESERVE = 50`, interactive/ad-hoc
`= 150`. With a 300 cap those are proportionally large, which is the point —
manual queries should yield to the scheduled scan.

---

## 6. Caching

SQLite, read-through, freshness windows enforced in `cache.py` and never in the
strategies:

| table | TTL |
|---|---|
| `finder_results` | 24h |
| `product_history` (watchlist) | 6h |
| `product_history` (general) | 24h |
| `category_tree` | 30d |
| `token_ledger` | append-only |

Store the raw JSON blob keyed by `(asin, domain, fetched_at)` so a scoring change
can be re-run over yesterday's data for **zero tokens**. This is what makes
threshold tuning practical — you will tune constantly in the first month, and
re-fetching each time would eat the entire daily budget.

---

## 7. Hetzner

**The 5/min refill rate promotes this from optional to necessary.** A nightly scan
is now a ~2-hour job that spends most of its life blocked waiting for tokens.
That is not something to run on a laptop that sleeps, and not something you want
to babysit. Build on Windows against recorded fixtures; run on Hetzner from the
first real nightly scan onward.

Setup:

- **CX22** (2 vCPU / 4 GB, ~€4/mo), Ubuntu 24.04. Nowhere near CPU-bound — this
  is an I/O-bound job that spends most of its life asleep waiting for token refill.
- **systemd timer, not cron** — real logs via `journalctl`, an `OnFailure=` hook
  for a failure email, and no silent 3am failures.
- `uv` for the environment; deploy by `git pull` + `systemctl restart`.
- Key in `/etc/keepa/env` mode 600, loaded via `EnvironmentFile=`. Not in the repo.
- SQLite lives on the server; `scp` the DB down when you want to analyse locally.
- Output: write `output/`, then email `summary.md` to yourself. Reviewing a
  shortlist on your phone at breakfast is the actual delivery mechanism.
- **One real hazard:** the Keepa token bucket is per *key*, not per machine. If
  the server is mid-run and you fire an ad-hoc query from Windows, one of them
  starves. The `KEEPA_TOKEN_RESERVE` floor handles it, but set the interactive
  reserve higher (say 1,500) than the nightly job's, so manual work yields to the
  scheduled scan rather than corrupting it.

---

## 8. Build order

1. `core/client.py` + `core/cache.py` + token ledger. Prove against one known
   ASIN; assert the Keepa-time epoch conversion in a test.
2. `core/series.py` with time-weighted window maths. Unit tests with synthetic
   step functions — this is where the subtle bugs live.
3. `core/fees.py` — non-VAT-registered model, fees grossed at 1.20 (§2.3).
4. `strategies/s02_amazon_oos.py`. Validate end-to-end against 3–5 ASINs you have
   eyeballed on keepa.com and can confirm by hand. Budget ~5 tokens each; record
   the responses as fixtures on the first call and never re-fetch them.
5. `core/report.py`, `excluded.csv`, `summary.md`.
6. **Stand up Hetzner here, before S1** — S2 alone is a ~1-hour paced job, and you
   want real nightly output accumulating while you build the second strategy.
7. `strategies/s01_niche_discovery.py` + `data/categories_uk.json` + rotation cursor.
8. Two weeks of nightly runs. Tune thresholds by re-scoring cached JSON, which is
   free; re-fetching to test a threshold change is a two-hour wait.
9. Then: S3 price-dip flips and S8 rank recovery (both reuse `series.py` almost
   entirely — cheapest next wins), S6 seasonal, S4 retail arbitrage, S5
   new-listing gaps, S7 bundles, S9 eBay (blocked on Terapeak exports regardless).

Per CLAUDE.md working style, each new strategy's filter logic gets sketched as a
comment and reviewed before implementation.

---

## 9. Settled and open

**Settled**

- Not VAT registered → fee model in §2.3.
- 300 token cap, 5/min refill → budget and pacing in §5.
- Buy box ownership is not the target; Amazon's own stock state is → §3.3.

**Still open**

1. **Which 5–10 UK leaf categories for Strategy 1?** Only blocks step 7. The scan
   is exactly as good as this list — unscoped runs produce noise.
2. **Per-unit capital ceiling.** Plan assumes £80 max unit cost and 5-unit test
   buys (~£400 committed, leaving reserve) against the £1,000 float.

---

## 10. Tracking API + webhook (later phase, deliberately)

Keepa's Tracking API is a push channel, separate from the polling client. Verified
in `structs/Tracking.java` and `TrackingRequest.java`:

- `NotifyIfType { OUT_OF_STOCK, BACK_IN_STOCK }`, registered per
  `(domainId, csvType)`. So `(domain 2, csv 0 AMAZON, OUT_OF_STOCK)` is a
  first-class trigger meaning *Amazon UK just went out of stock on this ASIN*.
  That is precisely Strategy 2's execution signal.
- `updateInterval` in hours (0-25, default 1) sets how often Keepa refreshes the
  tracked product.
- Notifications arrive either as an HTTP POST to a webhook URL, or by polling
  `tracking?type=notification&since=<keepaTime>` -- one call returns all pending
  notifications regardless of how many there are.
- Batch add supports up to 1000 trackings in one request.

**Cost model: a refill-rate tax, not a per-notification charge.** Every response
carries `tokenFlowReduction`, and active trackings reduce token flow rather than
billing per event. The exact formula is not in the public structs. The client now
records the field on every response, so the real number becomes visible the
moment the first tracking is added -- measure it before committing to a large
tracking list. On a 5/min budget, refill rate is the one resource least able to
absorb a tax.

**Why this is not built yet, and should not be:**

1. It is an *execution* tool, not a *discovery* one. You can only track ASINs you
   already know about, so it cannot replace or accelerate any scan.
2. There is nothing to alert on until inventory is actually held.
3. The webhook needs a public HTTPS endpoint, which does not exist until Hetzner
   is up. Polling notifications is cheap and works fine in the meantime.

**When it earns its place:** the moment the first Strategy 2 position is bought.
Then register the held ASINs with `OUT_OF_STOCK` (list now, at the gap price) and
`BACK_IN_STOCK` (the window is closing, Amazon is back). Keep the tracking list to
held inventory only -- tens of ASINs, not the scan universe.

Sequenced after step 6 (Hetzner) and the first real Strategy 2 shortlist.

**Incidental finding:** `Response.refillIn` documents that "tokens are generated
every 5 minutes", i.e. refill is chunked (25 at a time at 5/min), not smooth. The
client models it smoothly and resyncs from every response, which errs toward
waiting slightly too long rather than overdrawing. Acceptable, but it explains any
small discrepancy between predicted and actual availability.
