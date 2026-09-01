"""Tests for the Telegram digest.

No network. The properties that matter are behavioural, not cosmetic: a
notification must never take the scan down with it, the token must never appear
in output, and a quiet night must produce a quiet message.
"""

import pytest
import requests

from core import config, notify
from strategies.base import Candidate, ScanResult


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("KEEPA_TELEGRAM_BOT_TOKEN", "12345:SECRET_TOKEN_VALUE")
    monkeypatch.setenv("KEEPA_TELEGRAM_CHAT_ID", "999")
    monkeypatch.setattr(config, "_loaded", True)
    yield


def candidate(asin="B0TEST0001", score=100.0, title="Bamboo Drawer Organiser"):
    return Candidate(
        asin=asin, title=title, category="Storage", current_price_p=3200,
        score=score, strategy="s01", reason="sells at £32.00; 2 sellers",
        capital_required_p=16000, est_monthly_opportunity_p=5000,
        days_to_realise=9.0,
    )


def result(candidates):
    return ScanResult(candidates, [], tokens_spent=120)


# -- failing soft ---------------------------------------------------------


def test_a_network_failure_never_raises(monkeypatch):
    """Losing a message is annoying. Losing a two-hour scan because Telegram
    was briefly unreachable is not acceptable."""
    def boom(*a, **kw):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(notify.requests, "post", boom)
    assert notify.send("hello") is False


def test_a_non_200_is_reported_not_raised(monkeypatch):
    class Resp:
        status_code = 403

    monkeypatch.setattr(notify.requests, "post", lambda *a, **kw: Resp())
    assert notify.send("hello") is False


def test_missing_credentials_is_a_quiet_no_op(monkeypatch):
    monkeypatch.delenv("KEEPA_TELEGRAM_BOT_TOKEN", raising=False)
    assert notify.configured() is False
    assert notify.send("hello") is False


# -- the token must not leak ---------------------------------------------


def test_the_token_never_appears_in_the_message_body(monkeypatch):
    """The token lives in the URL. Anything that logs the request line leaks
    it -- which is exactly how the sportbet bot's token reaches this box's
    journal on every poll."""
    captured = {}

    class Resp:
        status_code = 200

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return Resp()

    monkeypatch.setattr(notify.requests, "post", fake_post)
    notify.send("a message")
    assert "SECRET_TOKEN_VALUE" in captured["url"], "token belongs in the URL"
    assert "SECRET_TOKEN_VALUE" not in str(captured["json"])


# -- what the digest says -------------------------------------------------


def test_a_quiet_night_is_a_short_message():
    """Most nights find nothing. A long message saying so is how a channel
    becomes noise the operator stops reading."""
    text = notify.scan_digest({"s02": result([])}, tokens_spent=305)
    assert "no candidates" in text
    assert len(text) < 300
    assert "305 tokens" in text


def test_candidates_carry_the_chart_link():
    """Every row is a 'look at this', and the chart is the final filter."""
    text = notify.scan_digest({"s01": result([candidate()])}, tokens_spent=120)
    assert "keepa.com/#!product/2-B0TEST0001" in text
    assert "Bamboo Drawer Organiser" in text
    assert "£160.00" in text, "capital required must be visible"


def test_the_digest_says_scores_are_ordinal():
    text = notify.scan_digest({"s01": result([candidate()])})
    assert "not a" in text.lower() and "buy this" in text.lower()


def test_only_the_top_rows_are_sent_and_the_rest_are_counted():
    many = [candidate(asin=f"B{i:09d}", score=float(i)) for i in range(9)]
    text = notify.scan_digest({"s01": result(many)})
    assert "and 4 more" in text
    assert text.count("chart</a>") == notify.TOP_N


def test_message_is_truncated_below_the_telegram_limit(monkeypatch):
    captured = {}

    class Resp:
        status_code = 200

    monkeypatch.setattr(
        notify.requests, "post",
        lambda url, json=None, timeout=None: (captured.update(json), Resp())[1],
    )
    notify.send("x" * 10_000)
    assert len(captured["text"]) <= notify.MAX_MESSAGE


def test_html_in_a_product_title_is_escaped():
    """Titles are arbitrary seller text; unescaped, a stray angle bracket
    breaks the message or worse."""
    text = notify.scan_digest(
        {"s01": result([candidate(title="Widget <b>PRO</b> & Co")])}
    )
    assert "&lt;b&gt;" in text
    assert "&amp;" in text


# -- failures always speak ------------------------------------------------


def test_failure_notification_is_sent_even_though_scans_are_usually_quiet(monkeypatch):
    sent = {}

    class Resp:
        status_code = 200

    monkeypatch.setattr(
        notify.requests, "post",
        lambda url, json=None, timeout=None: (sent.update(json), Resp())[1],
    )
    assert notify.notify_failure("Traceback: KeepaError") is True
    assert "FAILED" in sent["text"]
    assert "KeepaError" in sent["text"]
