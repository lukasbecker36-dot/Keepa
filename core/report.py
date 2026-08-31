"""Write a scan to disk: the ranked CSV, the excluded log, and summary.md.

Output conventions are CLAUDE.md's:
    output/YYYY-MM-DD/<strategy>.csv
    output/YYYY-MM-DD/excluded.csv
    output/YYYY-MM-DD/summary.md

Money is pence everywhere upstream and is formatted to GBP here, at the boundary
and nowhere else. Dates are Europe/London.

`keepa_url` is deliberately the first column after the title. It is the most
important field in the file: every row is a "look at this", not a "buy this",
and the operator's read of the chart is the final filter.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from .keepa_time import LONDON
from strategies.base import Candidate, ScanResult

OUTPUT_ROOT = Path("output")

BASE_COLUMNS = [
    "asin",
    "title",
    "keepa_url",
    "score",
    "current_price",
    "capital_required",
    "est_monthly_opportunity",
    "days_to_realise",
    "category",
    "reason",
    "notes",
    "strategy",
    "amazon_url",
]

# Columns whose values are pence and must be rendered as GBP.
PENCE_SUFFIXES = ("_p",)


def gbp(pence: int | float | None) -> str:
    if pence is None or pence == "":
        return ""
    return f"{pence / 100:.2f}"


def today_dir(root: Path = OUTPUT_ROOT, when: datetime | None = None) -> Path:
    stamp = (when or datetime.now(LONDON)).strftime("%Y-%m-%d")
    path = root / stamp
    path.mkdir(parents=True, exist_ok=True)
    return path


def _extra_columns(candidates: Sequence[Candidate]) -> list[str]:
    """Union of strategy-specific keys, stable order: first seen wins."""
    seen: list[str] = []
    for c in candidates:
        for key in c.extra:
            if key not in seen:
                seen.append(key)
    return seen


def _row(c: Candidate, extras: Sequence[str]) -> dict:
    row = {
        "asin": c.asin,
        "title": c.title,
        "keepa_url": c.keepa_url,
        "score": f"{c.score:.0f}",
        "current_price": gbp(c.current_price_p),
        "capital_required": gbp(c.capital_required_p),
        "est_monthly_opportunity": gbp(c.est_monthly_opportunity_p),
        "days_to_realise": (
            f"{c.days_to_realise:.0f}" if c.days_to_realise is not None else ""
        ),
        "category": c.category,
        "reason": c.reason,
        "notes": c.notes,
        "strategy": c.strategy,
        "amazon_url": c.amazon_url,
    }
    for key in extras:
        value = c.extra.get(key, "")
        # Any *_p key is pence; render it as money so the CSV needs no decoder.
        if key.endswith(PENCE_SUFFIXES) and isinstance(value, (int, float)):
            value = gbp(value)
        row[key] = value
    return row


def write_candidates(result: ScanResult, strategy: str, directory: Path) -> Path:
    extras = _extra_columns(result.candidates)
    path = directory / f"{strategy}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=BASE_COLUMNS + extras)
        writer.writeheader()
        for c in sorted(result.candidates, key=lambda x: x.score, reverse=True):
            writer.writerow(_row(c, extras))
    return path


def write_excluded(result: ScanResult, directory: Path) -> Path:
    """Excluded rows are kept, not discarded: the operator may hold approval for
    a brand they had not realised, and that row is then actionable."""
    path = directory / "excluded.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["asin", "title", "filter_name", "detail", "keepa_url"]
        )
        writer.writeheader()
        for e in result.excluded:
            writer.writerow(
                {
                    "asin": e.asin,
                    "title": e.title,
                    "filter_name": e.filter_name,
                    "detail": e.detail,
                    "keepa_url": f"https://keepa.com/#!product/2-{e.asin}",
                }
            )
    return path


def write_summary(
    results: dict[str, ScanResult], directory: Path, *, top_n: int = 10
) -> Path:
    """Human-readable digest. This is the file that gets read on a phone at
    breakfast, so it leads with what to do and why, not with statistics."""
    when = datetime.now(LONDON).strftime("%Y-%m-%d %H:%M %Z")
    lines = [f"# Scan summary — {when}", ""]

    total_tokens = sum(r.tokens_spent for r in results.values())
    total_cands = sum(len(r.candidates) for r in results.values())
    lines += [
        f"{total_cands} candidate(s) across {len(results)} strategy(ies); "
        f"{total_tokens} Keepa tokens spent.",
        "",
        "> Scores are **ordinal within this scan only** — a ranking, not a "
        "prediction. Every row is a *look at this*, not a *buy this*. "
        "Open the Keepa chart before acting on any of them.",
        "",
    ]

    for name, result in results.items():
        lines += [f"## {name}", ""]
        for note in result.notes:
            lines.append(f"- {note}")
        lines.append("")

        top = result.top(top_n)
        if not top:
            lines += [
                "**No candidates.** That is a result, not a failure — the "
                "filters rejected everything. See the reject reasons above for "
                "which threshold bound, and `excluded.csv` for rows that never "
                "reached scoring.",
                "",
            ]
            continue

        lines.append(f"Top {len(top)} of {len(result.candidates)}:")
        lines.append("")
        for i, c in enumerate(top, 1):
            lines.append(
                f"{i}. **[{c.asin}]({c.keepa_url})** — {c.title[:70]}  "
            )
            lines.append(f"   {c.reason}  ")
            lines.append(
                f"   capital £{gbp(c.capital_required_p)} · "
                f"est. £{gbp(c.est_monthly_opportunity_p)}/month"
                + (
                    f" · ~{c.days_to_realise:.0f}d to realise"
                    if c.days_to_realise is not None
                    else ""
                )
                + "  "
            )
            if c.notes:
                lines.append(f"   _{c.notes}_  ")
            lines.append("")

        if result.excluded:
            from collections import Counter

            counts = Counter(e.filter_name for e in result.excluded)
            lines.append(
                "Excluded by negative filters: "
                + ", ".join(f"{k} ({v})" for k, v in counts.most_common())
                + ". See `excluded.csv` — if you hold approval for a gated "
                "brand, those rows are actionable."
            )
            lines.append("")

    path = directory / "summary.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_all(
    results: dict[str, ScanResult], *, root: Path = OUTPUT_ROOT
) -> list[Path]:
    directory = today_dir(root)
    written = []
    combined_excluded: list = []
    for name, result in results.items():
        written.append(write_candidates(result, name, directory))
        combined_excluded.extend(result.excluded)
    if results:
        merged = ScanResult([], combined_excluded, 0)
        written.append(write_excluded(merged, directory))
    written.append(write_summary(results, directory))
    return written
