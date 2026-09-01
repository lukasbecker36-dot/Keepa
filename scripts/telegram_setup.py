"""Find your Telegram chat id and prove the bot can reach you.

    python -m scripts.telegram_setup --token 123456:ABC-DEF...

    # token already in .env as KEEPA_TELEGRAM_BOT_TOKEN:
    python -m scripts.telegram_setup

THE ONE GOTCHA
    A bot cannot start a conversation. Until you message it, Telegram has
    nothing to report and getUpdates returns an empty list -- which looks
    identical to a broken token. So: open Telegram, find your bot, send it
    anything (/start will do), THEN run this.

What it does, in order:
    1. checks the token is valid and names the bot           (getMe)
    2. reads recent messages and extracts the chat id        (getUpdates)
    3. sends a test message back, so you know it round-trips (sendMessage)
    4. prints the two lines to put in .env

Nothing here writes your token anywhere, and no output includes it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402

from core import config  # noqa: E402

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT_S = 20


def call(token: str, method: str, **params):
    """One API call. Errors never include the URL, which carries the token."""
    try:
        response = requests.post(
            API.format(token=token, method=method), json=params, timeout=TIMEOUT_S
        )
    except requests.RequestException as exc:
        return None, f"could not reach Telegram: {exc.__class__.__name__}"
    if response.status_code == 401:
        return None, "token rejected (401). Check it was copied whole, including the colon."
    if response.status_code != 200:
        body = response.json().get("description", "") if response.content else ""
        return None, f"HTTP {response.status_code}: {body}"
    payload = response.json()
    if not payload.get("ok"):
        return None, payload.get("description", "unknown error")
    return payload.get("result"), None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", help="bot token (default: KEEPA_TELEGRAM_BOT_TOKEN)")
    ap.add_argument("--no-test-message", action="store_true")
    args = ap.parse_args()

    token = args.token or config.get("KEEPA_TELEGRAM_BOT_TOKEN")
    if not token:
        print(
            "No token. Pass --token, or set KEEPA_TELEGRAM_BOT_TOKEN in .env.\n"
            "Get one from @BotFather in Telegram: /newbot",
            file=sys.stderr,
        )
        return 2

    # 1. Is the token real?
    me, error = call(token, "getMe")
    if error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    print(f"bot ok: @{me.get('username')} ({me.get('first_name')})")

    # A webhook silently swallows getUpdates -- worth naming, because the
    # symptom is an empty list that looks exactly like "you forgot to message".
    hook, _ = call(token, "getWebhookInfo")
    if hook and hook.get("url"):
        print(
            f"\nNOTE: a webhook is set ({hook['url']}), so getUpdates will stay "
            "empty.\nRemove it with deleteWebhook, or read the chat id from your "
            "webhook receiver.",
            file=sys.stderr,
        )
        return 1

    # 2. Who has talked to it?
    updates, error = call(token, "getUpdates", timeout=0)
    if error:
        print(f"FAILED reading updates: {error}", file=sys.stderr)
        return 1

    chats = {}
    for update in updates or []:
        message = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or {}
        )
        chat = message.get("chat") or {}
        if chat.get("id") is not None:
            name = (
                chat.get("username")
                or chat.get("title")
                or " ".join(
                    filter(None, [chat.get("first_name"), chat.get("last_name")])
                )
                or "?"
            )
            chats[chat["id"]] = (chat.get("type", "?"), name)

    if not chats:
        print(
            "\nNo messages yet, so Telegram has no chat id to report.\n"
            "A bot cannot start a conversation -- open Telegram, find "
            f"@{me.get('username')}, send it /start, then run this again.",
            file=sys.stderr,
        )
        return 1

    print(f"\nfound {len(chats)} chat(s):")
    for chat_id, (kind, name) in chats.items():
        print(f"  {chat_id}   {kind:<10} {name}")

    chat_id = next(iter(chats))
    if len(chats) > 1:
        print(f"\nUsing the first ({chat_id}). Pick another by hand if wrong.")

    # 3. Prove it round-trips. A chat id that reads correctly but cannot be
    #    sent to is worse than none, because it fails silently at 01:30.
    if not args.no_test_message:
        _, error = call(
            token,
            "sendMessage",
            chat_id=chat_id,
            text="✅ Keepa scanner connected. Nightly digests will arrive here.",
        )
        if error:
            print(f"\nFAILED to send: {error}", file=sys.stderr)
            return 1
        print("\ntest message sent — check Telegram.")

    print("\nAdd these to .env (local) and /opt/keepa/.env (server, mode 600):")
    print("  KEEPA_TELEGRAM_BOT_TOKEN=<your token>")
    print(f"  KEEPA_TELEGRAM_CHAT_ID={chat_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
