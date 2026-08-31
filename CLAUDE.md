# CLAUDE.md

Working brief for an Amazon UK sourcing and arbitrage toolkit built on the Keepa API, with eBay UK data as a secondary source. Owner is a first-time seller with ~£1,000 starting capital, operating merchant-fulfilled initially. Focus is analysis and shortlisting, not automated purchasing.

---

## Project goals

Build a set of nightly scanners and on-demand analysis scripts that surface actionable opportunities across nine strategies (below). Output is human-readable shortlists — CSV or markdown — that the operator reviews before acting. No auto-buying, no auto-listing.

Design for **decisions, not dashboards**. A run that returns 15 well-scored candidates beats one that returns 500 unranked rows.

---

## Environment

- Python 3.11+
- Keepa API key in `KEEPA_API_KEY` env var
- eBay Browse API OAuth credentials in `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET`
- SQLite for local persistence (`data/keepa.db`) — do not re-fetch data you already have within its freshness window
- Marketplace: Amazon UK (domain ID 2)
- All prices handled in pence internally to avoid float errors
- Output to `output/YYYY-MM-DD/<strategy>.csv` and a `summary.md`

---

## Data sources and their limits

**Keepa API** — primary source. Reliable: price history, sales rank history, Amazon in-stock/OOS flags, offer counts, buy box seller history from ~2021 onward, seller feedback. Approximate: sales estimates (directional, not exact; calibrate against own sold data over time). Gaps: buy box seller data pre-2021 has holes.

**eBay Browse API** — live listings only (title, price, seller feedback count, active listing counts, item condition). Free with a developer account. **No sold-price data** — the Marketplace Insights API that provides sold history is a Limited Release and is not accessible to individual developers. This is a hard constraint on any eBay-side strategy: we can see what's *asked*, not what's *paid*.

For eBay sold-price validation, the operator will export from **Terapeak** (in eBay Seller Hub, free) and drop CSVs into `data/terapeak/`. Scripts should read those when present rather than assume live sold data is available.

**Do not scrape eBay or Alibaba.** Terms of service, and detection risk. If a strategy requires scraped data, flag it and stop rather than building around it.

---

## Strategies to implement

Each strategy is one module under `strategies/`. Each exposes `scan()` returning a ranked list of candidates with a common schema (ASIN, title, current price, score, reason, capital required, estimated monthly opportunity, notes).

### 1. Niche discovery for private-label imports

Find Amazon UK listings where the top sellers are generic/unbranded and beatable on quality of listing.

Keepa Product Finder filters:
- Current price £25–£60 (below £25, fees eat margin; above £60, capital constraint)
- Sales rank current under 30,000 in a leaf category
- Rank 90-day average within 30% of current (stable, not spiking)
- Review count under 200 on top listings
- Item weight under 500g
- No batteries (exclude "battery" in title/features; check hazmat flags)
- Not Amazon as buy box seller

Score by: rank stability × (200 − avg review count of top 3) × (60 − current price). High score = stable demand, weak defence, room in the market.

Output: shortlist with a link to search Alibaba for the same product category. Do not auto-source.

### 2. Amazon out-of-stock arbitrage

Find products Amazon sells directly but routinely runs out of, where third-party sellers can capture the buy box at elevated prices.

Filters:
- Amazon has held buy box on this ASIN in at least 60% of 180-day history
- Amazon OOS ≥ 20% of last 90 days
- Median buy box price during Amazon OOS windows ≥ 40% above Amazon's own price
- Sales rank stays under 50,000 during OOS windows (proves sales occur at elevated price)
- Under 10 FBA sellers historically compete during gaps
- Current price ≥ some sane per-unit threshold (skip anything under £15 — fees don't work)

For each candidate, derive: average OOS window length in days, average price delta, estimated sales during gap windows using Keepa's sales estimate curve.

Score by: profit per unit at gap price × estimated gap-window unit sales per month.

**Note in output**: this strategy assumes FBA to win the buy box. Merchant-fulfilled will rarely win buy box against FBA competitors even when Amazon is out. Flag whether the operator can reach the ASIN merchant-fulfilled (i.e. Amazon OOS + no other FBA sellers).

### 3. Price-dip flips

Products currently at a transient low, expected to revert.

Filters:
- Current price in bottom 10% of 180-day range
- 180-day price range width ≥ 30% (needs volatility)
- Rank 90-day average under 50,000 (real ongoing demand)
- Current rank within 30% of 90-day average (dip is price-only, not demand collapse)
- Price has recovered from similar lows at least twice in 180 days

Score by: expected revert delta × 30-day sales estimate. Include capital required (current price × min viable order, usually 5 units) and days-to-revert estimate from prior recovery cycles.

Trap to avoid: structural price collapses. The "recovered twice" filter is the key defence — one-off dips often don't recover.

### 4. UK retail arbitrage

Amazon prices consistently above sale prices at Argos, Wilko, B&M, TK Maxx, The Range, Home Bargains.

Keepa can't see retailer prices directly. Approach: maintain a `data/retail_watchlist.csv` of ASINs the operator has manually matched to retail SKUs. Script pulls current Amazon buy box price for each and flags ones where the delta versus the recorded retail price crosses the profit threshold after fees.

Fees model (merchant-fulfilled): Amazon referral (15% for most categories, verify per category), £0.75 closing fee for media, no FBA fees since MF. Assume £2.95 Royal Mail 2nd class small parcel, £0.30 packaging.

Output: alert list of "buy X at Y, sell on Amazon at Z, net £N per unit."

### 5. New-listing gaps

Categories where top listings are all mature — a signal nobody's entered recently.

For a target list of leaf categories:
- Pull top 100 ASINs (Best Sellers query)
- Compute median listing age (from Keepa's first-tracked date)
- Flag categories where median age > 3 years AND at least one top-20 ASIN has review count under 500 (proving a smaller player broke in recently)

This surfaces the *category*, not a specific product. Output is a category shortlist for manual product-idea generation, not a buy list.

### 6. Seasonal price arbitrage

Products with reliable annual price cycles — buy in trough, hold, sell in peak.

Filters:
- 365-day price range width ≥ 50%
- Price low reliably occurs in the same 60-day window across 2+ prior years
- Price high reliably occurs 4+ months later
- Current date within 30 days of historical trough
- Sales rank at peak proves sell-through (not just aspirational pricing)

Score by: (peak price − current price − fees) × estimated units sellable at peak × 1/months_held (annualised).

Output warning: this strategy ties up capital for months. Include capital-lockup metric prominently.

### 7. Bundle creation

Complementary products where a combined listing you create could undercut the sum of individual prices.

Approach: pull "Frequently Bought Together" pairs for high-selling ASINs in target categories via Keepa's product data. For each pair, check whether either component is a private-label-able generic (see Strategy 1). If yes, source the generic version, bundle with the branded component, list as a new ASIN.

This is the strategy with the *most* room for a small operator because a new ASIN has no competition on day one. It's also the highest listing-creation workload.

Output: ranked pairs with sourcing feasibility notes.

### 8. Rank recovery

Products with strong historical rank that dropped due to a stockout or review wave and are recovering.

Filters:
- Rank was under 20,000 for 12+ consecutive months in the last 3 years
- Rank spiked above 100,000 for 30–90 days
- Rank has moved back under 50,000 in the last 30 days and is trending down (improving)
- Current price stable, offer count declining

Score by: distance from historical baseline rank × how quickly it's returning. These are products with proven demand that competitors have abandoned.

### 9. Amazon–eBay arbitrage

Two directions. Neither is dropshipping (buy-to-order after a sale) — eBay's business policies prohibit sourcing from another retailer to fulfil an eBay order, and enforcement is active. This means holding inventory both ways.

**eBay → Amazon:** find items currently listed cheap on eBay (buy-it-now, high seller feedback, new condition) that sell higher on Amazon. Match by title similarity and category. Filter for eBay price + eBay purchase fees + shipping ≤ Amazon current buy box × 0.65 (leaves room for Amazon fees plus margin). Requires the operator to buy on eBay, receive, then list on Amazon merchant-fulfilled.

**Amazon → eBay:** find items priced low on Amazon where eBay Terapeak sold data (operator-provided CSV) shows a consistent higher realised price. Filter for Amazon current price + Amazon purchase fees ≤ eBay median sold × 0.70 (leaves room for eBay fees plus margin).

Both directions require the operator to physically buy stock. Neither works without eBay sold data — for the eBay → Amazon direction, we validate the *Amazon* sell price via Keepa (which is fine). For the Amazon → eBay direction, we depend on Terapeak CSV imports; the script should refuse to score this strategy if no recent Terapeak data is loaded.

Fees model for eBay:
- Final value fee: ~13.4% including the fixed £0.30 per order (verify current rate; changes)
- Managed payments fee bundled into FVF
- Postage: same Royal Mail assumptions as Strategy 4

Beware: brand gating on Amazon applies to eBay → Amazon direction. Check restricted brand list before scoring.

---

## Negative filters (apply to every strategy)

Exclude ASINs matching any of these:

**Brand-gated on Amazon** (approval needed): Nike, Adidas, LEGO, Apple, Samsung, Sony, Disney, Hasbro, Mattel, Beats, most cosmetics and fragrance brands, most nutritional supplements. Maintain in `data/gated_brands.txt`; update quarterly.

**IP-hot categories**: Funko Pop, Pokémon cards and merchandise, sports memorabilia, anything with a licensed character.

**Compliance-heavy**: toys for under-14s (safety testing), cosmetics (CPSR requirement), food and supplements (FSA), electricals requiring UKCA (batteries, mains-powered), medical devices, CE-marked PPE.

**Hazmat**: anything Amazon flags as hazmat, batteries over 100Wh, aerosols, pressurised containers, flammables.

**Physical constraints for merchant-fulfilled**: weight over 2kg (postage kills margin), longest dimension over 45cm (Royal Mail medium parcel threshold), fragile without special packaging.

**Trademark risk**: any ASIN whose title contains a registered trademark unless the operator has authorisation. Default to "unknown = exclude" here.

---

## Data reliability notes

Fields to trust: current price, price history, sales rank history, offer count, Amazon in-stock flag, seller feedback count, first-tracked date.

Fields to treat as approximate: Keepa's monthly sales estimate (directional; a "1,000/month" estimate could plausibly be 400–2,000; useful for ranking, not for forecasting revenue). Rank-to-sales conversion differs sharply by category — the same rank means very different unit volumes in Toys vs Books.

Fields with gaps: buy box seller history before mid-2021 is incomplete. FBA-vs-MF flag on individual offers can lag reality by hours.

Fields to derive rather than trust: "profitability" numbers from any third-party tool. Always recompute using the operator's own fee model.

---

## Token budget and API discipline

Keepa base plan: 5 tokens/minute, ~7,200/day. Costs:
- Product Finder query: 10 tokens per call regardless of results
- Product lookup with history: 1 token per ASIN
- Best Sellers query: 50 tokens per category
- Deal query: variable, roughly 1 per deal returned

Daily budget guideline for a nightly run: 3,000–4,000 tokens, leaving headroom for ad-hoc queries during the day.

Discipline:
- Cache Product Finder results for 24h; niches don't shift by the hour
- Cache product history for 6h for active watchlist items, 24h otherwise
- Batch ASIN lookups in groups of 100 (Keepa max)
- Never re-fetch inside a single script run
- Log tokens used per strategy so it's clear which scans are expensive
- On low-token warning (<500 remaining), abort non-critical scans rather than degrade

eBay Browse API: 5,000 calls/day default limit, more than enough. Rate-limit self to 5 req/sec.

---

## Output conventions

Each strategy writes:
- `output/YYYY-MM-DD/<strategy>.csv` — full ranked candidates
- Top 10 rows summarised in `output/YYYY-MM-DD/summary.md` with a one-line reason per candidate
- Any candidate that scored highly but was excluded by a negative filter is logged separately in `output/YYYY-MM-DD/excluded.csv` with the filter that caught it — sometimes the operator can act on gated items they hadn't realised they had access to

Common CSV columns: `asin, title, category, current_price, score, strategy, reason, capital_required, est_monthly_opportunity, days_to_realise, notes, keepa_url, amazon_url`.

`keepa_url` should be `https://keepa.com/#!product/2-{ASIN}` — one click to the price chart for manual verification. This is the most important field; the operator's judgement on the chart is the final filter.

---

## What this project does not do

- Auto-purchase, auto-list, or send anything to Amazon Seller Central
- Store credit card or seller credentials
- Guarantee profit numbers — all scores are relative rankings within a scan, not predictions
- Handle VAT registration, EPR registration, WEEE, or any regulatory obligation
- Replace judgement on individual candidates. Every shortlist row is a "look at this," not a "buy this"

---

## Working style for Claude Code

- Before adding a new strategy, sketch the filter logic in a comment and stop for review
- Write the fee/cost model as a single module (`fees.py`) used by every strategy; do not duplicate fee logic
- Prefer explicit filter thresholds as named constants at the top of each strategy file, so tuning is one edit
- When a Keepa field is ambiguous, check the current Keepa API documentation rather than guessing
- All monetary computation in integer pence; format to £ only at output
- Timezone: Europe/London for all date logic
- Test each strategy against a known-good ASIN before running at scale
