"""SQLite persistence: response cache, token ledger, small key/value state.

At 5 tokens/minute a refetch is expensive in wall-clock time, so raw responses
are stored verbatim and forever. Re-scoring last night's JSON costs nothing and
returns instantly, which is what makes threshold tuning practical -- see PLAN.md
section 6. Never store parsed/derived values here; derive them on read.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DB = Path("data/keepa.db")

# Freshness windows, in seconds.
TTL_FINDER = 24 * 3600
TTL_PRODUCT_WATCHLIST = 6 * 3600
TTL_PRODUCT_GENERAL = 24 * 3600
TTL_CATEGORY_TREE = 30 * 24 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_product (
    asin        TEXT NOT NULL,
    domain      INTEGER NOT NULL,
    fetched_at  INTEGER NOT NULL,
    params      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    PRIMARY KEY (asin, domain, fetched_at)
);
CREATE INDEX IF NOT EXISTS idx_raw_product_lookup
    ON raw_product (asin, domain, fetched_at DESC);

CREATE TABLE IF NOT EXISTS finder_result (
    query_hash  TEXT NOT NULL,
    domain      INTEGER NOT NULL,
    fetched_at  INTEGER NOT NULL,
    selection   TEXT NOT NULL,
    asins       TEXT NOT NULL,
    PRIMARY KEY (query_hash, domain, fetched_at)
);
CREATE INDEX IF NOT EXISTS idx_finder_lookup
    ON finder_result (query_hash, domain, fetched_at DESC);

CREATE TABLE IF NOT EXISTS token_ledger (
    ts          INTEGER NOT NULL,
    strategy    TEXT,
    endpoint    TEXT NOT NULL,
    projected   INTEGER NOT NULL,
    consumed    INTEGER,
    tokens_left INTEGER,
    waited_s    REAL NOT NULL DEFAULT 0,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_ts ON token_ledger (ts DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def selection_hash(selection: dict) -> str:
    """Stable hash of a Product Finder selection, so an identical query hits cache
    regardless of dict ordering."""
    blob = json.dumps(selection, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class Cache:
    def __init__(self, path: Path | str = DEFAULT_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    # -- products ---------------------------------------------------------

    def put_product(
        self, asin: str, domain: int, payload: dict, params: dict | None = None
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO raw_product "
            "(asin, domain, fetched_at, params, payload) VALUES (?,?,?,?,?)",
            (
                asin,
                domain,
                int(time.time()),
                json.dumps(params or {}, sort_keys=True),
                json.dumps(payload, separators=(",", ":")),
            ),
        )

    def get_product(
        self, asin: str, domain: int, max_age_s: int | None = TTL_PRODUCT_GENERAL
    ) -> dict | None:
        """Most recent cached payload, or None if absent/stale.

        Pass max_age_s=None to read the newest copy regardless of age -- that is
        the offline re-scoring path, and it must never trigger a fetch.
        """
        row = self.conn.execute(
            "SELECT fetched_at, payload FROM raw_product "
            "WHERE asin=? AND domain=? ORDER BY fetched_at DESC LIMIT 1",
            (asin, domain),
        ).fetchone()
        if row is None:
            return None
        if max_age_s is not None and time.time() - row["fetched_at"] > max_age_s:
            return None
        return json.loads(row["payload"])

    def fresh_products(
        self, asins: list[str], domain: int, max_age_s: int
    ) -> tuple[dict[str, dict], list[str]]:
        """Split a request list into (already-fresh payloads, still-needed asins)."""
        hits: dict[str, dict] = {}
        misses: list[str] = []
        for asin in asins:
            got = self.get_product(asin, domain, max_age_s)
            if got is None:
                misses.append(asin)
            else:
                hits[asin] = got
        return hits, misses

    def product_history_rows(self, asin: str, domain: int) -> list[sqlite3.Row]:
        """Every stored fetch for an ASIN, newest first. Useful for diffing what
        changed between nightly runs without spending a token."""
        return list(
            self.conn.execute(
                "SELECT fetched_at, payload FROM raw_product "
                "WHERE asin=? AND domain=? ORDER BY fetched_at DESC",
                (asin, domain),
            )
        )

    # -- finder -----------------------------------------------------------

    def put_finder(self, selection: dict, domain: int, asins: list[str]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO finder_result "
            "(query_hash, domain, fetched_at, selection, asins) VALUES (?,?,?,?,?)",
            (
                selection_hash(selection),
                domain,
                int(time.time()),
                json.dumps(selection, sort_keys=True),
                json.dumps(asins),
            ),
        )

    def get_finder(
        self, selection: dict, domain: int, max_age_s: int | None = TTL_FINDER
    ) -> list[str] | None:
        row = self.conn.execute(
            "SELECT fetched_at, asins FROM finder_result "
            "WHERE query_hash=? AND domain=? ORDER BY fetched_at DESC LIMIT 1",
            (selection_hash(selection), domain),
        ).fetchone()
        if row is None:
            return None
        if max_age_s is not None and time.time() - row["fetched_at"] > max_age_s:
            return None
        return json.loads(row["asins"])

    # -- token ledger -----------------------------------------------------

    def log_tokens(
        self,
        endpoint: str,
        projected: int,
        consumed: int | None,
        tokens_left: int | None,
        *,
        strategy: str | None = None,
        waited_s: float = 0.0,
        note: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO token_ledger "
            "(ts, strategy, endpoint, projected, consumed, tokens_left, waited_s, note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                int(time.time()),
                strategy,
                endpoint,
                projected,
                consumed,
                tokens_left,
                waited_s,
                note,
            ),
        )

    def tokens_spent_since(self, since_ts: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(COALESCE(consumed, projected)), 0) AS n "
            "FROM token_ledger WHERE ts >= ?",
            (since_ts,),
        ).fetchone()
        return int(row["n"])

    def spend_by_strategy(self, since_ts: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT strategy, SUM(COALESCE(consumed, projected)) AS tokens, "
                "SUM(waited_s) AS waited, COUNT(*) AS calls "
                "FROM token_ledger WHERE ts >= ? GROUP BY strategy "
                "ORDER BY tokens DESC",
                (since_ts,),
            )
        )

    # -- meta -------------------------------------------------------------

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
            (key, json.dumps(value)),
        )

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row["value"]) if row else default
