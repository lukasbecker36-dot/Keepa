"""Keepa HTTP client with a token governor.

Written against the REST API directly rather than the `keepa` PyPI package. The
binding constraint on this project is a 300-token bucket refilling at 5/min
(PLAN.md section 5), and that needs exact control over when a request is issued,
how large a batch is, and what happens when the bucket runs dry. A convenience
wrapper that decides those for us is a liability here, not a help.

The governor models the bucket locally between calls and resynchronises from
every response. It BLOCKS rather than failing: a job that aborts halfway has
wasted the tokens it already spent.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import requests

from . import config
from .cache import Cache, TTL_FINDER, TTL_PRODUCT_GENERAL

API_BASE = "https://api.keepa.com"
DOMAIN_UK = 2

# Bucket parameters. Confirmed for this account: 300 cap, 5/min.
DEFAULT_BUCKET_MAX = 300
DEFAULT_REFILL_PER_MIN = 5

# Keepa accepts at most 100 ASINs per /product call.
MAX_ASINS_PER_REQUEST = 100

# Product Finder rejects perPage below 50 with "combination of perPage and page
# exceeds limit or is too small" (HTTP 400, costs no tokens). Paging is also
# bounded: page * perPage must stay within Keepa's result ceiling.
MIN_PER_PAGE = 50
MAX_FINDER_RESULTS = 10_000

# Reserve floors. The nightly job runs lean; interactive work yields to it.
RESERVE_NIGHTLY = 50
RESERVE_INTERACTIVE = 150

# Refuse to block longer than this in one wait; beyond it the run window is
# blown and a partial report is the better outcome.
DEFAULT_MAX_WAIT_S = 45 * 60

# Measured against the live API, not assumed (see docs/PLAN.md section 2.1):
#   Product Finder                       11 tokens/page (the docs say 10)
#   product + history + stats             1 token/ASIN  (stats is free)
#   product + history + stats + buybox    3 tokens/ASIN (buybox adds 2, not 4)
FINDER_TOKEN_COST = 11
BUYBOX_EXTRA_PER_ASIN = 2
RATING_EXTRA_PER_ASIN = 1   # measured: rating=1 makes a 1-token fetch cost 2

# Wait until at least this many items are affordable before issuing a batch.
# Without it, a run that starts with a nearly-empty bucket dribbles one ASIN per
# refill tick -- 50 HTTP calls where 1 would do. Same tokens, far more requests
# and a much longer wall clock. Set to 1 to disable.
MIN_WORTHWHILE_BATCH = 25

HTTP_TIMEOUT_S = 60
MAX_RETRIES = 4


class KeepaError(RuntimeError):
    pass


class TokenBudgetExceeded(KeepaError):
    """Raised when satisfying a request would exceed the run's token ceiling, or
    would require waiting longer than the caller allows."""


@dataclass
class Bucket:
    """Local model of Keepa's token bucket, resynced from every response."""

    tokens: float
    refill_per_min: float = DEFAULT_REFILL_PER_MIN
    maximum: int = DEFAULT_BUCKET_MAX
    updated_at: float = field(default_factory=time.time)

    def available(self, now: float | None = None) -> float:
        """Projected tokens right now. Capped at `maximum` -- the bucket does not
        accumulate past its cap, so idling overnight does not bank a big burst."""
        now = time.time() if now is None else now
        elapsed_min = max(now - self.updated_at, 0.0) / 60.0
        return min(self.tokens + elapsed_min * self.refill_per_min, float(self.maximum))

    def sync(self, tokens_left: float, refill_per_min: float | None = None) -> None:
        self.tokens = tokens_left
        if refill_per_min:
            self.refill_per_min = refill_per_min
        self.maximum = max(self.maximum, int(tokens_left))
        self.updated_at = time.time()

    def seconds_until(self, needed: float, now: float | None = None) -> float:
        """Wall-clock seconds until `needed` tokens are available."""
        have = self.available(now)
        if have >= needed:
            return 0.0
        if self.refill_per_min <= 0:
            return float("inf")
        return (needed - have) / self.refill_per_min * 60.0


class KeepaClient:
    def __init__(
        self,
        api_key: str | None = None,
        cache: Cache | None = None,
        *,
        domain: int = DOMAIN_UK,
        reserve: int = RESERVE_INTERACTIVE,
        max_tokens: int | None = None,
        strategy: str | None = None,
        dry_run: bool = False,
        max_wait_s: float = DEFAULT_MAX_WAIT_S,
        sleep: Callable[[float], None] = time.sleep,
        session: requests.Session | None = None,
    ) -> None:
        config.load_env()
        key = api_key or config.get("KEEPA_API_KEY")
        if not key and not dry_run:
            config.require_api_key()  # raises MissingApiKey with setup instructions
        self.api_key = key or ""
        self.cache = cache or Cache()
        self.domain = domain
        self.reserve = reserve
        self.max_tokens = max_tokens
        self.strategy = strategy
        self.dry_run = dry_run
        self.max_wait_s = max_wait_s
        self._sleep = sleep
        self.session = session or requests.Session()

        # Restore the bucket from the last known server state rather than
        # assuming a full one. The token bucket belongs to the API KEY, not to
        # this process: a fresh client that assumes 300 available will overdraw
        # whenever another run -- or an earlier run of this one -- has just
        # spent. That is exactly how a 49-ASIN batch drove tokensLeft to -44.
        maximum = int(config.get("KEEPA_BUCKET_MAX", DEFAULT_BUCKET_MAX))
        refill = float(config.get("KEEPA_REFILL_PER_MIN", DEFAULT_REFILL_PER_MIN))
        self.bucket = Bucket(tokens=float(maximum), refill_per_min=refill,
                             maximum=maximum)
        saved = self.cache.get_meta("bucket_state")
        if saved:
            elapsed_min = max(time.time() - saved.get("at", 0), 0) / 60.0
            self.bucket.tokens = min(
                saved.get("tokens", maximum)
                + elapsed_min * saved.get("refill_per_min", refill),
                float(maximum),
            )
            self.bucket.refill_per_min = saved.get("refill_per_min", refill)
            self.bucket.updated_at = time.time()
        self.spent = 0
        self.waited_s = 0.0
        # Set from live responses; non-zero once Tracking API subscriptions are
        # active. See _absorb().
        self.token_flow_reduction = 0.0

    # -- governor ---------------------------------------------------------

    def remaining_budget(self) -> int | None:
        if self.max_tokens is None:
            return None
        return max(self.max_tokens - self.spent, 0)

    def _authorise(self, projected: int) -> float:
        """Block until `projected` tokens are safely spendable. Returns seconds
        waited. Raises TokenBudgetExceeded rather than overspending."""
        budget = self.remaining_budget()
        if budget is not None and projected > budget:
            raise TokenBudgetExceeded(
                f"call needs {projected} tokens, run budget has {budget} left "
                f"(--max-tokens {self.max_tokens})"
            )
        if projected > self.bucket.maximum - self.reserve:
            raise TokenBudgetExceeded(
                f"call needs {projected} tokens but the bucket holds at most "
                f"{self.bucket.maximum} and reserve is {self.reserve}; "
                f"split the request into smaller batches"
            )

        needed = projected + self.reserve
        wait = self.bucket.seconds_until(needed)
        if wait <= 0:
            return 0.0
        if wait > self.max_wait_s:
            raise TokenBudgetExceeded(
                f"would need to wait {wait / 60:.1f} min for {projected} tokens "
                f"(limit {self.max_wait_s / 60:.1f} min); write a partial report instead"
            )
        self._sleep(wait)
        self.waited_s += wait
        return wait

    def affordable_batch(self, wanted: int, cost_per_item: int = 1) -> int:
        """Largest batch spendable from the bucket right now, without waiting.

        Bucket size caps batches independently of Keepa's 100-ASIN limit: at a
        300 cap with a 150 reserve, 100 ASINs is not always affordable even
        though the API would accept it.
        """
        spare = self.bucket.available() - self.reserve
        by_bucket = int(max(spare, 0) // max(cost_per_item, 1))
        budget = self.remaining_budget()
        by_budget = wanted if budget is None else int(budget // max(cost_per_item, 1))
        return max(0, min(wanted, by_bucket, by_budget, MAX_ASINS_PER_REQUEST))

    # -- transport --------------------------------------------------------

    def _request(
        self, endpoint: str, params: dict[str, Any], projected: int, note: str = ""
    ) -> dict:
        if self.dry_run:
            self.spent += projected
            self.cache.log_tokens(
                endpoint, projected, None, None,
                strategy=self.strategy, note=f"DRY RUN {note}".strip(),
            )
            return {"products": [], "asinList": [], "_dry_run": True}

        waited = self._authorise(projected)
        params = {"key": self.api_key, **params}

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.get(
                    f"{API_BASE}/{endpoint}", params=params, timeout=HTTP_TIMEOUT_S
                )
            except requests.RequestException as exc:  # transport, not quota
                last_error = exc
                self._sleep(min(2**attempt, 30))
                continue

            if resp.status_code == 429:
                # Overdrawn despite the local model -- trust the server. Assume
                # the bucket is empty and wait out the full cost from zero.
                last_error = KeepaError(
                    f"429 rate limited on {endpoint}; local bucket model was "
                    f"optimistic by at least {projected} tokens"
                )
                self.bucket.sync(0.0)
                back_off = self.bucket.seconds_until(projected + self.reserve) or 60.0
                self._sleep(min(back_off, self.max_wait_s))
                self.waited_s += back_off
                continue
            if resp.status_code >= 500:
                last_error = KeepaError(f"keepa {resp.status_code}")
                self._sleep(min(2**attempt, 30))
                continue
            if resp.status_code != 200:
                raise KeepaError(f"keepa HTTP {resp.status_code}: {resp.text[:400]}")

            payload = resp.json()
            if payload.get("error"):
                raise KeepaError(f"keepa error: {payload['error']}")

            self._absorb(payload, endpoint, projected, waited, note)
            return payload

        raise KeepaError(f"{endpoint} failed after {MAX_RETRIES} attempts: {last_error}")

    def _absorb(
        self, payload: dict, endpoint: str, projected: int, waited: float, note: str
    ) -> None:
        """Resync the bucket from the response and record the spend."""
        tokens_left = payload.get("tokensLeft")
        consumed = payload.get("tokensConsumed")
        refill_rate = payload.get("refillRate")
        if tokens_left is not None:
            self.bucket.sync(float(tokens_left), refill_rate)
            # Persist so the next process starts from reality, not from 300.
            self.cache.set_meta("bucket_state", {
                "tokens": float(tokens_left),
                "refill_per_min": self.bucket.refill_per_min,
                "at": time.time(),
            })
        self.spent += int(consumed if consumed is not None else projected)

        # Keepa taxes active Tracking API subscriptions by reducing the token
        # refill rate rather than billing per notification. Record it: on a
        # 5/min budget, the cost of a tracking list shows up here and nowhere
        # else, and we want the real number before committing to trackings.
        reduction = payload.get("tokenFlowReduction")
        if reduction:
            self.token_flow_reduction = float(reduction)
            note = f"{note} flowReduction={reduction}".strip()

        self.cache.log_tokens(
            endpoint,
            projected,
            consumed,
            tokens_left,
            strategy=self.strategy,
            waited_s=waited,
            note=note or None,
        )

    # -- endpoints --------------------------------------------------------

    def product(
        self,
        asins: Sequence[str],
        *,
        stats_days: int | None = 180,
        history: bool = True,
        offers: int | None = None,
        buybox: bool = False,
        rating: bool = False,
        max_age_s: int | None = TTL_PRODUCT_GENERAL,
    ) -> dict[str, dict]:
        """Fetch products, cache-first, batched and paced.

        Cost is 1 token/ASIN with history and stats. `offers` (6 tokens per 10)
        and `buybox` (5 tokens/ASIN) are supported but off by default and should
        stay off: neither Strategy 1 nor Strategy 2 needs them, and enabling
        buybox alone would 5x every scan. See PLAN.md section 2.1.
        """
        asins = [a.strip().upper() for a in asins if a and a.strip()]
        results, missing = self.cache.fresh_products(list(asins), self.domain, max_age_s) \
            if max_age_s is not None else ({}, list(asins))

        cost_each = 1
        if buybox:
            cost_each += BUYBOX_EXTRA_PER_ASIN
        if rating:
            # csv[16] RATING and csv[17] COUNT_REVIEWS are absent without this;
            # Strategy 1 cannot work from the free payload.
            cost_each += RATING_EXTRA_PER_ASIN
        if offers:
            cost_each += (offers + 9) // 10 * 6

        i = 0
        while i < len(missing):
            remaining = len(missing) - i
            take = self.affordable_batch(remaining, cost_each)
            # Accumulate rather than dribble: wait for a worthwhile batch unless
            # that is all that is left, or the wait would breach the run window.
            target = min(remaining, MIN_WORTHWHILE_BATCH, MAX_ASINS_PER_REQUEST)
            if take < target:
                wanted = target * cost_each
                if self.bucket.seconds_until(wanted + self.reserve) <= self.max_wait_s:
                    self._authorise(wanted)
                    take = self.affordable_batch(remaining, cost_each)
            if take == 0:
                self._authorise(cost_each)
                continue
            batch = missing[i : i + take]
            i += take

            params: dict[str, Any] = {
                "domain": self.domain,
                "asin": ",".join(batch),
                "history": 1 if history else 0,
            }
            if stats_days:
                params["stats"] = stats_days
            if offers:
                params["offers"] = offers
            if buybox:
                params["buybox"] = 1
            if rating:
                params["rating"] = 1

            payload = self._request(
                "product", params, projected=cost_each * len(batch),
                note=f"{len(batch)} asins",
            )
            for product in payload.get("products") or []:
                asin = product.get("asin")
                if not asin:
                    continue
                self.cache.put_product(asin, self.domain, product, params={
                    "stats": stats_days, "history": history,
                    "offers": offers, "buybox": buybox, "rating": rating,
                })
                results[asin] = product

        return results

    def product_finder(
        self,
        selection: dict,
        *,
        page: int = 0,
        per_page: int = 50,
        max_age_s: int | None = TTL_FINDER,
    ) -> list[str]:
        """Run one Product Finder query. Flat 10 tokens regardless of result count.

        Returns ASINs only -- the finder does not return product data, so a
        second `product()` pass is always required.
        """
        if per_page < MIN_PER_PAGE:
            raise ValueError(
                f"perPage must be at least {MIN_PER_PAGE}; Keepa rejects smaller "
                f"pages with a 400. Ask for {MIN_PER_PAGE} and slice locally."
            )
        if (page + 1) * per_page > MAX_FINDER_RESULTS:
            raise ValueError(
                f"page {page} at perPage {per_page} exceeds Keepa's "
                f"{MAX_FINDER_RESULTS} result ceiling"
            )
        query = {**selection, "page": page, "perPage": per_page}
        if max_age_s is not None:
            cached = self.cache.get_finder(query, self.domain, max_age_s)
            if cached is not None:
                return cached

        payload = self._request(
            "query",
            {"domain": self.domain, "selection": json.dumps(query, sort_keys=True)},
            projected=FINDER_TOKEN_COST,
            note=f"page {page}",
        )
        asins = payload.get("asinList") or []
        # A dry run returns an empty stub. Caching it would poison the 24h TTL
        # and make the next real run silently return nothing.
        if not payload.get("_dry_run"):
            self.cache.put_finder(query, self.domain, asins)
        return asins

    def product_finder_pages(
        self, selection: dict, *, pages: int, per_page: int = 50
    ) -> list[str]:
        """Consecutive finder pages, de-duplicated, stopping early on a short page."""
        seen: list[str] = []
        known: set[str] = set()
        for page in range(pages):
            batch = self.product_finder(selection, page=page, per_page=per_page)
            for asin in batch:
                if asin not in known:
                    known.add(asin)
                    seen.append(asin)
            if len(batch) < per_page:
                break
        return seen

    # -- reporting --------------------------------------------------------

    def summary(self) -> str:
        parts = [
            f"tokens spent={self.spent}",
            f"bucket~{self.bucket.available():.0f}/{self.bucket.maximum}",
            f"refill={self.bucket.refill_per_min:g}/min",
            f"waited={self.waited_s / 60:.1f}min",
        ]
        if self.token_flow_reduction:
            parts.append(f"flowReduction={self.token_flow_reduction:g}")
        return "  ".join(parts)
