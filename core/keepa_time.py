"""Keepa time conversions.

Keepa expresses every timestamp as *minutes since 2011-01-01T00:00:00Z*. Getting
this constant wrong shifts every window by a year without raising anything, so it
is asserted in tests/test_keepa_time.py against a hand-checked date.

All date logic in the project is Europe/London per CLAUDE.md, but Keepa's series
are UTC. Convert to London only at the output boundary, never inside window maths
-- BST transitions would otherwise silently move gap boundaries by an hour.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Unix seconds at 2011-01-01T00:00:00Z, expressed in minutes.
KEEPA_EPOCH_MINUTES = 21_564_000
KEEPA_EPOCH_SECONDS = KEEPA_EPOCH_MINUTES * 60  # 1_293_840_000

LONDON = ZoneInfo("Europe/London")

MINUTES_PER_HOUR = 60
MINUTES_PER_DAY = 1440


def keepa_to_unix(keepa_minutes: int) -> int:
    """Keepa minutes -> unix seconds (UTC)."""
    return (int(keepa_minutes) + KEEPA_EPOCH_MINUTES) * 60


def unix_to_keepa(unix_seconds: float) -> int:
    """Unix seconds -> Keepa minutes, truncated."""
    return int(unix_seconds // 60) - KEEPA_EPOCH_MINUTES


def keepa_to_datetime(keepa_minutes: int, tz: ZoneInfo | None = None) -> datetime:
    """Keepa minutes -> aware datetime. Defaults to UTC; pass LONDON for display."""
    dt = datetime.fromtimestamp(keepa_to_unix(keepa_minutes), tz=timezone.utc)
    return dt.astimezone(tz) if tz else dt


def datetime_to_keepa(dt: datetime) -> int:
    """Aware datetime -> Keepa minutes. Naive datetimes are rejected rather than
    assumed to be UTC; a silent assumption here is a whole-day error class."""
    if dt.tzinfo is None:
        raise ValueError("datetime_to_keepa requires an aware datetime")
    return unix_to_keepa(dt.timestamp())


def now_keepa() -> int:
    """Current time in Keepa minutes."""
    return unix_to_keepa(time.time())


def days_ago_keepa(days: float, *, now: int | None = None) -> int:
    """Keepa minute value `days` before now (or before an injected `now`)."""
    ref = now_keepa() if now is None else now
    return ref - int(days * MINUTES_PER_DAY)


def format_london(keepa_minutes: int) -> str:
    """Human-readable Europe/London timestamp, for report output only."""
    return keepa_to_datetime(keepa_minutes, LONDON).strftime("%Y-%m-%d %H:%M %Z")
