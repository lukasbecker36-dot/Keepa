# Keepa UK sourcing toolkit

Nightly scanners over the Keepa API that produce ranked shortlists for manual
review. Amazon UK (domain 2), merchant-fulfilled, not VAT registered.

See [docs/PLAN.md](docs/PLAN.md) for the design and the strategy specifications.

---

## Setup

### 1. Install dependencies

```powershell
python -m pip install -r requirements.txt
```

### 2. Add your Keepa API key

**Edit the `.env` file in this folder** and replace the placeholder:

```
KEEPA_API_KEY=your_key_here      <- replace this with your actual key
```

so it reads, for example:

```
KEEPA_API_KEY=a1b2c3d4e5f6g7h8i9
```

That is the whole step. `.env` is already created and already gitignored, so the
key will not be committed.

Get the key from <https://keepa.com/#!api> → *Manage API key*.

<details>
<summary>Alternative: set it as a Windows environment variable instead</summary>

If you would rather not keep it in a file — useful on the Hetzner box later, or
if you run scripts from several folders:

```powershell
setx KEEPA_API_KEY "a1b2c3d4e5f6g7h8i9"
```

Then **open a new terminal**. `setx` writes to your user profile and does not
affect the window you typed it in, which is the usual reason this appears not to
work.

A real environment variable always takes precedence over `.env`.
</details>

### 3. Check it works

```powershell
python -m scripts.verify_live --asin B0XXXXXXXX
```

Use any Amazon UK ASIN (the `B0...` code in a product URL). One where Amazon
itself goes in and out of stock exercises the gap detection best.

This costs about 1 token and confirms the two assumptions that would otherwise
corrupt everything downstream: that timestamps decode to real dates, and that the
buy-box series is being read with the right stride. It compares our computed
Amazon out-of-stock percentage against Keepa's own server-side figure — if those
agree, the window maths is right.

The response is saved to `tests/fixtures/` so it never needs fetching twice.

If the key is missing you get instructions rather than a stack trace.

---

## Other settings in `.env`

| Key | Default | Meaning |
|---|---|---|
| `KEEPA_API_KEY` | — | Required. Your Keepa key. |
| `KEEPA_DOMAIN` | `2` | Amazon marketplace. 2 is UK. |
| `KEEPA_BUCKET_MAX` | `300` | Token bucket cap for your plan. |
| `KEEPA_REFILL_PER_MIN` | `5` | Token refill rate for your plan. |

The last two matter: the client paces every request against them. If you upgrade
your Keepa plan, change them here and scans get faster automatically. The client
also learns a larger bucket from live responses, but starting from the right
numbers avoids unnecessary waiting on the first run.

---

## Running the tests

```powershell
python -m pytest tests/ -q
```

No network and no tokens — everything runs against synthetic data and fixtures.

---

## Token discipline

The bucket is **300 tokens, refilling at 5/min**, shared across every process
using the key. That is the binding constraint on the whole project:

- Sustained throughput is 300 tokens/hour. The bucket does not accumulate past
  its cap, so idling overnight does not bank a burst.
- A 100-ASIN fetch costs 100 tokens and takes 20 minutes to earn back.
- A nightly scan is a paced ~2-hour job that spends most of its time blocked.

**Do not develop against the live API.** Work from the fixtures in
`tests/fixtures/` and the cached payloads in `data/keepa.db`. Re-scoring cached
data is free and instant; refetching is a two-hour wait. One careless debug loop
costs you the night's scan.

Every call is logged to the `token_ledger` table with what it cost and how long
it waited.

---

## Layout

```
core/
  config.py      .env loading and key validation
  client.py      Keepa HTTP client + token governor
  cache.py       SQLite: raw payloads, finder results, token ledger
  series.py      csv[] decoding and time-weighted window maths
  csv_types.py   Keepa CsvType table (stride matters -- see the module docstring)
  keepa_time.py  Keepa-minutes <-> datetime
strategies/      one module per strategy (not yet built)
scripts/         verify_live.py and other one-offs
docs/PLAN.md     design and strategy specs
```
