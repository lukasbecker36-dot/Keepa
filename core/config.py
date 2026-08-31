"""Configuration loading: reads `.env` from the project root into os.environ.

Deliberately dependency-free -- python-dotenv would be one more thing to install
on the Hetzner box for about twenty lines of parsing.

Precedence is: real environment variables win over `.env`. That way the server
can use a systemd EnvironmentFile and the same code runs unchanged locally.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"

# Values that mean "you copied .env.example and forgot to edit it".
PLACEHOLDERS = {"", "your_key_here", "changeme", "xxx"}

_loaded = False


def load_env(path: Path | None = None, *, force: bool = False) -> dict[str, str]:
    """Parse `.env` into os.environ without clobbering existing variables.

    Idempotent: safe to call from anywhere, runs its parse once.
    """
    global _loaded
    if _loaded and not force:
        return {}
    env_path = path or ENV_FILE
    found: dict[str, str] = {}
    if env_path.is_file():
        for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            found[key] = value
            # A real environment variable always wins.
            if key not in os.environ:
                os.environ[key] = value
    _loaded = True
    return found


def get(name: str, default: str | None = None) -> str | None:
    """Read a setting, treating placeholder values as absent."""
    load_env()
    value = os.environ.get(name)
    if value is None or value.strip().lower() in PLACEHOLDERS:
        return default
    return value


def require_api_key() -> str:
    """Return the Keepa key, or explain precisely how to set it."""
    key = get("KEEPA_API_KEY")
    if key:
        return key
    raise MissingApiKey(
        "KEEPA_API_KEY is not set.\n"
        "\n"
        "Pick either option:\n"
        "\n"
        f"  1. Edit {ENV_FILE}\n"
        "     and replace the placeholder on the KEEPA_API_KEY line:\n"
        "         KEEPA_API_KEY=abc123yourrealkey\n"
        "\n"
        "  2. Or set it permanently for your Windows user (PowerShell, once):\n"
        "         setx KEEPA_API_KEY \"abc123yourrealkey\"\n"
        "     then open a NEW terminal -- setx does not affect the current one.\n"
        "\n"
        "Your key is at https://keepa.com/#!api (Manage API key).\n"
        ".env is gitignored, so the key will not be committed."
    )


class MissingApiKey(RuntimeError):
    pass
