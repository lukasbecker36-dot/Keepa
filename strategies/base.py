"""Shared shape for every strategy.

`Candidate` matches the common CSV columns in CLAUDE.md, so `report.py` never
needs to know which strategy produced a row. Strategy-specific measurements go
in `extra`, which the writer appends as additional columns.

Money is pence everywhere in this module. Formatting to GBP happens once, in the
report writer, and nowhere else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

KEEPA_URL = "https://keepa.com/#!product/{domain}-{asin}"
AMAZON_URL = "https://www.amazon.co.uk/dp/{asin}"


@dataclass(frozen=True)
class Candidate:
    asin: str
    title: str
    category: str
    current_price_p: int
    score: float
    strategy: str
    reason: str
    capital_required_p: int
    est_monthly_opportunity_p: int
    days_to_realise: float | None
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def keepa_url(self) -> str:
        """The most important field in the output. Every shortlist row is a
        'look at this', and the operator's read of the chart is the final
        filter -- see CLAUDE.md."""
        return KEEPA_URL.format(domain=2, asin=self.asin)

    @property
    def amazon_url(self) -> str:
        return AMAZON_URL.format(asin=self.asin)


@dataclass(frozen=True)
class Excluded:
    """A candidate that scored but was rejected by a negative filter.

    Kept and reported separately: the operator may turn out to have access to a
    gated brand they had not realised, and that row is then actionable.
    """

    asin: str
    title: str
    filter_name: str
    detail: str = ""


@dataclass
class ScanResult:
    candidates: list[Candidate]
    excluded: list[Excluded]
    tokens_spent: int
    notes: list[str] = field(default_factory=list)

    def top(self, n: int = 10) -> list[Candidate]:
        return sorted(self.candidates, key=lambda c: c.score, reverse=True)[:n]


class Strategy(ABC):
    name: str
    description: str

    @abstractmethod
    def scan(self) -> ScanResult:
        """Return ranked candidates. Must not spend tokens beyond the client's
        configured budget, and must return partial results rather than raising
        when the budget runs out."""
