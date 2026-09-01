"""CLI entry point.

    python run.py --strategy s02                    full nightly scan
    python run.py --strategy s02 --pages 1          a cheap look
    python run.py --strategy s02 --dry-run          plan the spend, spend nothing
    python run.py --rescore                         re-score the cache, 0 tokens

`--rescore` is the one to reach for while tuning. At 5 tokens/min a refetch is an
hour; re-scoring yesterday's cached JSON is instant and free, and it is the only
sane way to move a threshold and see what changes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core import config, filters, notify, report
from core.cache import Cache
from core.client import (
    RESERVE_INTERACTIVE,
    RESERVE_NIGHTLY,
    KeepaClient,
    TokenBudgetExceeded,
)
from strategies.base import Candidate, Excluded, ScanResult

STRATEGIES = {"s02": "s02_amazon_oos", "s01": "s01_niche_discovery"}


def build(name: str, client: KeepaClient, pages: int | None, verify: int):
    if name == "s02":
        from strategies.s02_amazon_oos import AmazonOosStrategy, FINDER_PAGES

        return AmazonOosStrategy(
            client, pages=pages or FINDER_PAGES, verify_top_n=verify
        )
    if name == "s01":
        from strategies.s01_niche_discovery import (
            FINDER_PAGES as S01_PAGES,
            NicheDiscoveryStrategy,
        )

        return NicheDiscoveryStrategy(client, pages=pages or S01_PAGES)
    raise SystemExit(f"unknown strategy {name!r}; known: {', '.join(STRATEGIES)}")


def rescore(strategy: str) -> ScanResult:
    """Re-run analysis over cached payloads. Spends nothing, touches no network."""
    from strategies import s02_amazon_oos as s02

    cache = Cache()
    asins = [
        r["asin"]
        for r in cache.conn.execute(
            "SELECT DISTINCT asin FROM raw_product WHERE domain=2"
        )
    ]
    products = [p for p in (cache.get_product(a, 2, None) for a in asins) if p]
    kept, dropped = filters.RESALE.partition(products)

    candidates: list[Candidate] = []
    excluded = [
        Excluded(p.get("asin", ""), p.get("title") or "",
                 v.primary.filter_name if v.primary else "", v.reason())
        for p, v in dropped
    ]
    rejects: dict[str, int] = {}
    for product in kept:
        a = s02.analyse(product)
        if a.passed:
            candidates.append(s02.to_candidate(product, a))
        else:
            key = a.rejected.split(":")[0]
            rejects[key] = rejects.get(key, 0) + 1

    candidates.sort(key=lambda c: c.score, reverse=True)
    notes = [
        f"RE-SCORED FROM CACHE — no tokens spent, no buy-box verification.",
        f"{len(products)} cached products, {len(kept)} passed filters, "
        f"{len(candidates)} scored",
    ]
    if rejects:
        notes.append(
            "rejects: "
            + ", ".join(
                f"{k}={v}"
                for k, v in sorted(rejects.items(), key=lambda kv: -kv[1])[:8]
            )
        )
    return ScanResult(candidates, excluded, 0, notes)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="s02", choices=sorted(STRATEGIES))
    ap.add_argument("--pages", type=int, help="finder pages (default per strategy)")
    ap.add_argument("--verify", type=int, default=20, help="buybox-verify top N")
    ap.add_argument("--max-tokens", type=int, help="hard ceiling for this run")
    ap.add_argument("--nightly", action="store_true", help="use the lower reserve")
    ap.add_argument("--dry-run", action="store_true", help="plan, spend nothing")
    ap.add_argument("--rescore", action="store_true", help="re-score cache, free")
    ap.add_argument(
        "--notify", action="store_true",
        help="send a Telegram digest when the run finishes",
    )
    args = ap.parse_args()

    if args.rescore:
        if args.strategy != "s02":
            print("--rescore currently supports s02 only", file=sys.stderr)
            return 2
        result = rescore(args.strategy)
    else:
        try:
            client = KeepaClient(
                cache=Cache(),
                strategy=args.strategy,
                reserve=RESERVE_NIGHTLY if args.nightly else RESERVE_INTERACTIVE,
                max_tokens=args.max_tokens,
                dry_run=args.dry_run,
            )
        except config.MissingApiKey as exc:
            print(exc, file=sys.stderr)
            return 2

        print(f"start: {client.summary()}", flush=True)
        strategy = build(args.strategy, client, args.pages, args.verify)
        try:
            result = strategy.scan()
        except TokenBudgetExceeded as exc:
            print(f"stopped: {exc}", file=sys.stderr)
            return 1
        print(f"end:   {client.summary()}", flush=True)

    written = report.write_all({STRATEGIES[args.strategy]: result})
    print(f"\n{len(result.candidates)} candidates, {len(result.excluded)} excluded")
    for path in written:
        print(f"  wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
