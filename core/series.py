"""Decode Keepa `csv[]` history into queryable step functions.

Keepa emits a data point only when a value *changes*. A price held for 40 days
and a price held for 40 minutes are therefore each a single sample. Any
sample-weighted statistic over this data is wrong -- consistently, and without
any visible symptom. Every aggregate in this module is TIME-weighted.

That is the whole point of the module, and the reason it is worth its own tests.

Usage:
    hist = ProductHistory.from_product(payload)
    amazon = hist[csv_types.AMAZON]
    gaps = amazon.runs(is_missing, t0, t1, min_minutes=24 * 60)
    bb = hist[csv_types.BUY_BOX_SHIPPING]
    price_in_gap = bb.weighted_median(gaps[0].start, gaps[0].end)
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Callable, Iterator

from . import csv_types
from .keepa_time import MINUTES_PER_DAY, now_keepa

Predicate = Callable[[int], bool]


def is_missing(value: int) -> bool:
    """True where Keepa recorded no offer. For csv[0] this is Amazon OOS."""
    return value < 0


def is_present(value: int) -> bool:
    return value >= 0


@dataclass(frozen=True)
class Window:
    """A half-open interval [start, end) in Keepa minutes."""

    start: int
    end: int

    @property
    def minutes(self) -> int:
        return self.end - self.start

    @property
    def days(self) -> float:
        return self.minutes / MINUTES_PER_DAY

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"window ends before it starts: {self.start}..{self.end}")


@dataclass(frozen=True)
class Segment:
    """A constant-valued stretch of a step function."""

    start: int
    end: int
    value: int

    @property
    def minutes(self) -> int:
        return self.end - self.start


class Series:
    """One decoded `csv[]` entry as a right-continuous step function.

    The value at time t is that of the most recent point at or before t. The
    final point extends to the end of whatever window is queried, which is how
    Keepa intends open-ended current state to be read.
    """

    __slots__ = ("csv_index", "times", "values", "shipping")

    def __init__(
        self,
        csv_index: int,
        times: list[int],
        values: list[int],
        shipping: list[int] | None = None,
    ) -> None:
        if len(times) != len(values):
            raise ValueError("times and values must be the same length")
        self.csv_index = csv_index
        self.times = times
        self.values = values
        self.shipping = shipping

    # -- construction -----------------------------------------------------

    @classmethod
    def from_csv(
        cls,
        raw: list[int] | None,
        csv_index: int,
        *,
        combine_shipping: bool = True,
    ) -> "Series":
        """Decode one raw csv[] array.

        Stride comes from the CsvType table, never from a guess: shipping-bearing
        series are [time, price, shipping] triplets and the rest are pairs.

        With combine_shipping (the default) a triplet series yields the landed
        total the buyer actually pays, which is what every downstream comparison
        wants. A shipping value of -1 means unknown and is treated as zero; the
        raw component stays available on `.shipping`.
        """
        meta = csv_types.BY_INDEX.get(csv_index)
        if meta is None:
            raise KeyError(f"unknown csv index {csv_index}")
        stride = meta.stride

        times: list[int] = []
        values: list[int] = []
        ship: list[int] | None = [] if stride == 3 else None

        if raw:
            if len(raw) % stride:
                raise ValueError(
                    f"csv[{csv_index}] ({meta.name}) has {len(raw)} entries, "
                    f"not a multiple of stride {stride}"
                )
            for i in range(0, len(raw), stride):
                t = raw[i]
                v = raw[i + 1]
                if stride == 3:
                    s = raw[i + 2]
                    ship.append(s)  # type: ignore[union-attr]
                    if combine_shipping and v >= 0:
                        v = v + (s if s > 0 else 0)
                times.append(t)
                values.append(v)

            for a, b in zip(times, times[1:]):
                if b < a:
                    raise ValueError(f"csv[{csv_index}] timestamps are not ascending")

        return cls(csv_index, times, values, ship)

    # -- basic access -----------------------------------------------------

    def __len__(self) -> int:
        return len(self.times)

    def __bool__(self) -> bool:
        return bool(self.times)

    @property
    def name(self) -> str:
        return csv_types.name_for(self.csv_index)

    @property
    def first_time(self) -> int | None:
        return self.times[0] if self.times else None

    @property
    def last_time(self) -> int | None:
        return self.times[-1] if self.times else None

    def at(self, t: int) -> int | None:
        """Value in force at time t, or None if t precedes all recorded data."""
        if not self.times:
            return None
        i = bisect_right(self.times, t) - 1
        return self.values[i] if i >= 0 else None

    def current(self) -> int | None:
        """Most recent recorded value."""
        return self.values[-1] if self.values else None

    # -- windowing --------------------------------------------------------

    def segments(self, t0: int, t1: int) -> Iterator[Segment]:
        """Constant-valued segments clipped to [t0, t1), in time order.

        The last recorded point extends to t1. Callers must not pass a t1 beyond
        `now`, or that final value gets credited with time it has not yet held.
        """
        if t1 <= t0 or not self.times:
            return
        n = len(self.times)
        start_i = max(bisect_right(self.times, t0) - 1, 0)
        for i in range(start_i, n):
            seg_start = self.times[i]
            seg_end = self.times[i + 1] if i + 1 < n else t1
            if seg_end <= t0:
                continue
            if seg_start >= t1:
                break
            yield Segment(max(seg_start, t0), min(seg_end, t1), self.values[i])

    def runs(
        self,
        predicate: Predicate,
        t0: int,
        t1: int,
        *,
        min_minutes: int = 0,
    ) -> list[Window]:
        """Contiguous windows in [t0, t1) where predicate(value) holds.

        Adjacent qualifying segments merge. `min_minutes` drops short runs --
        for Amazon stock gaps this is what separates a real selling window from
        a restock blip.
        """
        out: list[Window] = []
        start: int | None = None
        end = t0
        for seg in self.segments(t0, t1):
            if predicate(seg.value):
                if start is None:
                    start = seg.start
                end = seg.end
            elif start is not None:
                if end - start >= min_minutes:
                    out.append(Window(start, end))
                start = None
        if start is not None and end - start >= min_minutes:
            out.append(Window(start, end))
        return out

    def coverage(self, t0: int, t1: int, *, drop_missing: bool = True) -> float:
        """Fraction of [t0, t1) for which a usable value exists.

        Low coverage makes every other statistic over the window untrustworthy.
        Gate on this before believing a median.
        """
        span = t1 - t0
        if span <= 0:
            return 0.0
        held = sum(
            seg.minutes
            for seg in self.segments(t0, t1)
            if not (drop_missing and is_missing(seg.value))
        )
        return held / span

    # -- time-weighted aggregates ----------------------------------------

    def _weights(self, t0: int, t1: int, drop_missing: bool) -> dict[int, int]:
        weights: dict[int, int] = {}
        for seg in self.segments(t0, t1):
            if drop_missing and is_missing(seg.value):
                continue
            if seg.minutes <= 0:
                continue
            weights[seg.value] = weights.get(seg.value, 0) + seg.minutes
        return weights

    def weighted_median(
        self, t0: int, t1: int, *, drop_missing: bool = True
    ) -> int | None:
        """Time-weighted median over the window, or None if no usable data.

        This is the project's default summary statistic. Use it in preference to
        the mean: Keepa series are full of brief extreme excursions (a single
        mispriced offer for an hour) and the median ignores them by construction.
        """
        weights = self._weights(t0, t1, drop_missing)
        if not weights:
            return None
        total = sum(weights.values())
        acc = 0
        for value in sorted(weights):
            acc += weights[value]
            if acc * 2 >= total:
                return value
        return None  # unreachable

    def weighted_mean(
        self, t0: int, t1: int, *, drop_missing: bool = True
    ) -> float | None:
        weights = self._weights(t0, t1, drop_missing)
        if not weights:
            return None
        total = sum(weights.values())
        return sum(v * w for v, w in weights.items()) / total

    def min_in(self, t0: int, t1: int, *, drop_missing: bool = True) -> int | None:
        weights = self._weights(t0, t1, drop_missing)
        return min(weights) if weights else None

    def max_in(self, t0: int, t1: int, *, drop_missing: bool = True) -> int | None:
        weights = self._weights(t0, t1, drop_missing)
        return max(weights) if weights else None

    def missing_fraction(self, t0: int, t1: int) -> float:
        """Fraction of the window with no offer. For csv[0], Amazon's OOS rate.

        Cross-check this against the Finder's outOfStockPercentage90 on a few
        ASINs -- they should agree closely, and a disagreement means the window
        maths is wrong.
        """
        span = t1 - t0
        if span <= 0:
            return 0.0
        missing = sum(
            seg.minutes for seg in self.segments(t0, t1) if is_missing(seg.value)
        )
        return missing / span

    def drop_events(
        self, t0: int, t1: int, *, min_drop_pct: float = 0.10
    ) -> list[int]:
        """Timestamps where the value fell sharply.

        On a SALES series a sharp rank improvement is Keepa's standard proxy for
        a unit having sold: rank is a decaying average, so it only steps down
        when a purchase happens. Small drift is filtered out by min_drop_pct.

        Returns the timestamp at which each drop was recorded, so a caller can
        ask what some OTHER series read at that moment -- which is how you find
        out what price a unit actually sold at.
        """
        events: list[int] = []
        prev: int | None = None
        for i, t in enumerate(self.times):
            value = self.values[i]
            if t >= t1:
                break
            if value < 0:
                prev = None
                continue
            if prev is not None and prev > 0 and t >= t0:
                if (prev - value) / prev >= min_drop_pct:
                    events.append(t)
            prev = value
        return events

    def drops_in_windows(
        self, windows: list[Window], *, min_drop_pct: float = 0.10
    ) -> list[int]:
        out: list[int] = []
        for w in windows:
            out.extend(self.drop_events(w.start, w.end, min_drop_pct=min_drop_pct))
        return out

    def median_over_windows(
        self, windows: list[Window], *, drop_missing: bool = True
    ) -> int | None:
        """Time-weighted median across several disjoint windows at once.

        Strategy 2 needs "the typical buy box price across all Amazon stock gaps",
        which is this -- not the mean of per-gap medians, which would weight a
        two-day gap the same as a two-month one.
        """
        weights: dict[int, int] = {}
        for w in windows:
            for value, minutes in self._weights(w.start, w.end, drop_missing).items():
                weights[value] = weights.get(value, 0) + minutes
        if not weights:
            return None
        total = sum(weights.values())
        acc = 0
        for value in sorted(weights):
            acc += weights[value]
            if acc * 2 >= total:
                return value
        return None


def price_at_sales(
    price: "Series",
    rank: "Series",
    windows: list[Window],
    *,
    min_drop_pct: float = 0.10,
) -> tuple[int | None, int]:
    """What units ACTUALLY sold for, and how many sold.

    Takes each sales-rank drop as one sale, reads the price in force at that
    instant, and returns the median of those prices with the event count.

    This is the difference between a price being ASKED and a price being PAID.
    A time-weighted median of the price series says an item was listed at £84
    for most of a stock gap; it cannot say whether anyone bought at £84. If no
    rank drop occurred while that price stood, nobody did, and the £84 is a
    phantom -- one seller's hopeful ask with no market behind it.

    The median here is deliberately NOT time-weighted: each sale is one
    observation regardless of how long its price happened to be displayed.
    """
    prices: list[int] = []
    events = rank.drops_in_windows(windows, min_drop_pct=min_drop_pct)
    for t in events:
        value = price.at(t)
        if value is not None and value >= 0:
            prices.append(value)
    if not prices:
        return None, len(events)
    prices.sort()
    mid = len(prices) // 2
    if len(prices) % 2:
        return prices[mid], len(events)
    return (prices[mid - 1] + prices[mid]) // 2, len(events)


class ProductHistory:
    """All decoded series for one product, keyed by csv index."""

    __slots__ = ("asin", "series", "raw", "tracked_from")

    def __init__(
        self,
        asin: str,
        series: dict[int, Series],
        raw: dict | None = None,
        tracked_from: int | None = None,
    ):
        self.asin = asin
        self.series = series
        self.raw = raw
        # Keepa minute at which Keepa began tracking this ASIN. Before it, there
        # is no data -- which is NOT the same as "in stock". See window().
        self.tracked_from = tracked_from

    @classmethod
    def from_product(cls, product: dict, *, combine_shipping: bool = True):
        """Build from one element of a Keepa /product response's `products` list."""
        csv = product.get("csv") or []
        series: dict[int, Series] = {}
        for idx, raw in enumerate(csv):
            if raw is None or idx not in csv_types.BY_INDEX:
                continue
            series[idx] = Series.from_csv(
                raw, idx, combine_shipping=combine_shipping
            )
        tracked = product.get("trackingSince")
        return cls(
            product.get("asin", ""),
            series,
            product,
            tracked_from=tracked if tracked and tracked > 0 else None,
        )

    def __getitem__(self, csv_index: int) -> Series:
        """Always returns a Series -- an absent series is an empty one, so
        callers never branch on None just to ask a question about a window."""
        got = self.series.get(csv_index)
        if got is None:
            return Series(csv_index, [], [])
        return got

    def __contains__(self, csv_index: int) -> bool:
        return bool(self.series.get(csv_index))

    def window(self, days: float, *, now: int | None = None) -> Window:
        """Trailing window of `days`, clamped to the period Keepa has tracked.

        The clamp is not cosmetic. For an ASIN tracked 56 days, a raw 90-day
        window contains 34 days of *absence of data*, and absence is not the
        same as "Amazon was in stock". Counting it as in-stock understated the
        out-of-stock percentage by a third on the first live product we tried.

        Keepa's own stats.outOfStockPercentage90 clamps this way, and the
        Product Finder pre-filters on Keepa's definition -- so using a different
        one in the second pass would filter inconsistently against the first.
        """
        end = now_keepa() if now is None else now
        start = end - int(days * MINUTES_PER_DAY)
        if self.tracked_from is not None and self.tracked_from > start:
            start = self.tracked_from
        return Window(min(start, end), end)

    def tracked_days(self, *, now: int | None = None) -> float | None:
        """How long Keepa has tracked this ASIN. Short histories make every
        percentage noisy -- gate on it before trusting a rate."""
        if self.tracked_from is None:
            return None
        end = now_keepa() if now is None else now
        return (end - self.tracked_from) / MINUTES_PER_DAY
