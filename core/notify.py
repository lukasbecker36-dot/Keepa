"""Telegram notification for scan results.

A nightly scan that writes files nobody reads is a scan that may as well not
run. This is the delivery mechanism: a digest to a phone, which is where a
shortlist actually gets reviewed.

Design decisions worth stating:

**A notification failure must never fail the scan.** Every call here is wrapped
and returns a bool. Losing a message is annoying; losing a two-hour run because
Telegram was briefly unreachable is not acceptable.

**Silence is the default when there is nothing to say.** Across every strategy,
most nights find nothing -- that is the honest base rate, not a fault. A message
every night saying "0 candidates" trains the reader to ignore the channel, so a
zero-result run sends one short line and a run with candidates sends the detail.
Failures always send.

**The token is never logged.** The bot token appears in the URL, so any logging
of the request line leaks it -- which is exactly how the sportbet bot's token
ends up in this box's journal on every poll. Nothing here logs a URL.
"""

from __future__ import annotations

import html
from typing import Sequence

import requests

from . import config
from .report import gbp

API = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT_S = 15
# Telegram rejects messages over 4096 characters.
MAX_MESSAGE = 3900
TOP_N = 5


def _credentials() -> tuple[str | None, str | None]:
    return config.get("KEEPA_TELEGRAM_BOT_TOKEN"), config.get("KEEPA_TELEGRAM_CHAT_ID")


def configured() -> bool:
    token, chat = _credentials()
    return bool(token and chat)


def send(text: str, *, disable_preview: bool = True) -> bool:
    """Send one message. Returns False on any failure, never raises.

    The token goes in the URL, so nothing here prints or logs it -- not even on
    error, where the temptation to dump the request is strongest.
    """
    token, chat = _credentials()
    if not token or not chat:
        return False
    try:
        response = requests.post(
            API.format(token=token),
            json={
                "chat_id": chat,
                "text": text[:MAX_MESSAGE],
                "parse_mode": "HTML",
                "disable_web_page_preview": disable_preview,
            },
            timeout=TIMEOUT_S,
        )
        return response.status_code == 200
    except requests.RequestException:
        return False


def _row(index: int, candidate) -> str:
    """One candidate as a couple of lines, with the chart link.

    The Keepa link is the point: every row is a "look at this", and the chart is
    the final filter (CLAUDE.md). A digest without it is not actionable.
    """
    title = html.escape((candidate.title or "")[:60])
    reason = html.escape(candidate.reason[:150])
    return (
        f"\n<b>{index}. {title}</b>\n"
        f"{reason}\n"
        f'£{gbp(candidate.capital_required_p)} capital · '
        f'<a href="{candidate.keepa_url}">chart</a>\n'
    )


def scan_digest(results: dict, *, tokens_spent: int = 0) -> str:
    """Build the message for a completed run."""
    total = sum(len(r.candidates) for r in results.values())
    if not total:
        # Deliberately terse. Most nights find nothing, and a long message
        # saying so is how a channel becomes noise.
        strategies = ", ".join(results)
        return (
            f"🔍 Keepa scan: <b>no candidates</b> ({strategies}), "
            f"{tokens_spent} tokens. Filters rejected everything — that is the "
            f"usual outcome, not a fault."
        )

    lines = [f"🔍 <b>Keepa scan: {total} candidate(s)</b> · {tokens_spent} tokens\n"]
    for name, result in results.items():
        top = result.top(TOP_N)
        if not top:
            continue
        lines.append(f"\n<b>— {html.escape(name)} —</b>")
        for i, candidate in enumerate(top, 1):
            lines.append(_row(i, candidate))
        if len(result.candidates) > len(top):
            lines.append(f"…and {len(result.candidates) - len(top)} more in the CSV\n")
    lines.append(
        "\n<i>Scores rank within this scan only. Every row is a "
        "“look at this”, not a “buy this” — open the chart first.</i>"
    )
    return "".join(lines)


def notify_scan(results: dict, *, tokens_spent: int = 0) -> bool:
    if not configured():
        return False
    return send(scan_digest(results, tokens_spent=tokens_spent))


def notify_failure(detail: str) -> bool:
    """Always sent. A silent failure is the one outcome with no recovery path:
    the operator believes a scan ran when it did not."""
    if not configured():
        return False
    return send(f"⚠️ <b>Keepa scan FAILED</b>\n<pre>{html.escape(detail[:1500])}</pre>")
