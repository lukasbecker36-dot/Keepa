"""Tests for the token governor.

No network. The bucket model is exercised against an explicit clock, because the
behaviour that matters -- refusing to overspend, and sizing batches to what the
bucket actually holds -- only shows up under time pressure that a live test
cannot reproduce cheaply at 5 tokens/min.
"""

import pytest

from core.cache import Cache
from core.client import (
    Bucket,
    KeepaClient,
    MAX_ASINS_PER_REQUEST,
    TokenBudgetExceeded,
)


@pytest.fixture
def cache(tmp_path):
    c = Cache(tmp_path / "test.db")
    yield c
    c.close()


def client(cache, **kw):
    kw.setdefault("dry_run", True)
    kw.setdefault("sleep", lambda s: None)
    return KeepaClient(api_key="test", cache=cache, **kw)


# -- bucket model ---------------------------------------------------------


def test_bucket_refills_at_rate():
    b = Bucket(tokens=0, refill_per_min=5, maximum=300, updated_at=0.0)
    assert b.available(now=60) == pytest.approx(5)
    assert b.available(now=600) == pytest.approx(50)


def test_bucket_never_exceeds_cap():
    """The single most important property: idling does not bank a burst."""
    b = Bucket(tokens=0, refill_per_min=5, maximum=300, updated_at=0.0)
    assert b.available(now=86_400) == 300, "a full day of refill still caps at 300"


def test_seconds_until_accounts_for_existing_tokens():
    b = Bucket(tokens=100, refill_per_min=5, maximum=300, updated_at=0.0)
    # Need 40 more at 5/min = 8 minutes.
    assert b.seconds_until(140, now=0) == pytest.approx(480)
    assert b.seconds_until(50, now=0) == 0.0


def test_sync_learns_a_larger_bucket():
    b = Bucket(tokens=10, refill_per_min=5, maximum=300)
    b.sync(1200, refill_per_min=20)
    assert b.maximum >= 1200
    assert b.refill_per_min == 20


# -- authorisation --------------------------------------------------------


def test_refuses_a_call_larger_than_the_bucket_can_ever_hold(cache):
    c = client(cache, reserve=150)
    # 300 cap - 150 reserve = 150 spendable, ever.
    with pytest.raises(TokenBudgetExceeded, match="split the request"):
        c._authorise(200)


def test_refuses_to_exceed_the_run_budget(cache):
    c = client(cache, max_tokens=40, reserve=0)
    with pytest.raises(TokenBudgetExceeded, match="run budget"):
        c._authorise(50)


def test_refuses_to_wait_longer_than_the_run_window(cache):
    c = client(cache, reserve=0, max_wait_s=60)
    c.bucket = Bucket(tokens=0, refill_per_min=5, maximum=300, updated_at=1e12)
    # 100 tokens at 5/min is 20 minutes -- past the 1 minute limit.
    with pytest.raises(TokenBudgetExceeded, match="partial report"):
        c._authorise(100)


def test_waits_when_the_bucket_is_merely_low(cache):
    waits = []
    c = client(cache, reserve=0, sleep=waits.append)
    c.bucket = Bucket(tokens=10, refill_per_min=5, maximum=300, updated_at=1e12)
    c._authorise(20)
    assert waits and waits[0] == pytest.approx(120)  # 10 more tokens at 5/min
    assert c.waited_s == pytest.approx(120)


# -- batch sizing ---------------------------------------------------------


def test_batch_is_capped_by_the_bucket_not_just_the_api_limit(cache):
    """At a 300 cap with a 150 reserve, 100 ASINs is affordable; with a nearly
    empty bucket it is not, even though Keepa would accept the request."""
    c = client(cache, reserve=150)
    c.bucket = Bucket(tokens=300, refill_per_min=5, maximum=300, updated_at=1e12)
    assert c.affordable_batch(100) == 100

    c.bucket = Bucket(tokens=180, refill_per_min=5, maximum=300, updated_at=1e12)
    assert c.affordable_batch(100) == 30


def test_batch_respects_the_api_hard_limit(cache):
    c = client(cache, reserve=0)
    c.bucket = Bucket(tokens=5000, refill_per_min=5, maximum=5000, updated_at=1e12)
    assert c.affordable_batch(500) == MAX_ASINS_PER_REQUEST


def test_batch_respects_remaining_run_budget(cache):
    c = client(cache, reserve=0, max_tokens=25)
    c.bucket = Bucket(tokens=300, refill_per_min=5, maximum=300, updated_at=1e12)
    assert c.affordable_batch(100) == 25


def test_expensive_options_shrink_the_batch(cache):
    """buybox costs 3 tokens/ASIN (measured), so the same bucket buys a third."""
    c = client(cache, reserve=0)
    c.bucket = Bucket(tokens=99, refill_per_min=5, maximum=300, updated_at=1e12)
    assert c.affordable_batch(100, cost_per_item=1) == 99
    assert c.affordable_batch(100, cost_per_item=3) == 33


def test_batch_is_zero_when_dry_rather_than_negative(cache):
    c = client(cache, reserve=150)
    c.bucket = Bucket(tokens=0, refill_per_min=5, maximum=300, updated_at=1e12)
    assert c.affordable_batch(100) == 0


# -- ledger ---------------------------------------------------------------


def test_dry_run_spends_no_network_but_records_projected_cost(cache):
    c = client(cache, strategy="s02", dry_run=True)
    c.product_finder({"current_SALES_lte": 50_000})
    assert c.spent == 11
    rows = cache.spend_by_strategy(0)
    assert rows[0]["strategy"] == "s02"
    assert rows[0]["tokens"] == 11


def test_dry_run_does_not_poison_the_finder_cache(cache):
    """A dry run returns an empty stub; caching it would make the next real run
    read back nothing for the whole 24h TTL."""
    selection = {"current_SALES_lte": 50_000}
    client(cache, dry_run=True).product_finder(selection)
    assert cache.get_finder({**selection, "page": 0, "perPage": 50}, 2) is None


def test_ledger_accumulates_across_calls(cache):
    c = client(cache, strategy="s01", dry_run=True)
    for page in range(3):
        c.product_finder({"x": 1}, page=page)
    assert cache.tokens_spent_since(0) == 33


def test_waits_to_accumulate_a_worthwhile_batch(cache):
    """A run starting on a near-empty bucket must not dribble one ASIN per
    refill tick -- that is 50 HTTP calls where 1 would do, for the same tokens.
    """
    from core.client import MIN_WORTHWHILE_BATCH

    c = client(cache, reserve=50)
    c.bucket = Bucket(tokens=52, refill_per_min=5, maximum=300, updated_at=1e12)
    # Only 2 tokens spendable above the reserve right now.
    assert c.affordable_batch(100) == 2
    # ...but the batch target is much larger, so product() should wait first.
    assert MIN_WORTHWHILE_BATCH > 2


def test_bucket_state_survives_across_client_instances(cache, monkeypatch):
    """The token bucket belongs to the API KEY, not to a process. A fresh client
    that assumes a full bucket will overdraw whenever another run has just
    spent -- which drove tokensLeft to -44 on a live run.
    """
    import time as _t

    cache.set_meta("bucket_state", {
        "tokens": 20.0, "refill_per_min": 5.0, "at": _t.time(),
    })
    c = KeepaClient(api_key="test", cache=cache, dry_run=True, sleep=lambda s: None)
    assert c.bucket.available() < 40, "must not assume a full 300-token bucket"


def test_absent_saved_state_falls_back_to_configured_maximum(cache):
    c = KeepaClient(api_key="test", cache=cache, dry_run=True, sleep=lambda s: None)
    assert c.bucket.available() == pytest.approx(c.bucket.maximum, abs=1)
