"""Tests for .env loading and the missing-key error path.

The failure this guards against is a confusing one: an unedited .env still
contains `your_key_here`, which would otherwise be sent to Keepa as a real key
and come back as an opaque auth error.
"""

import os

import pytest

from core import config


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    monkeypatch.delenv("KEEPA_API_KEY", raising=False)
    monkeypatch.setattr(config, "_loaded", False)
    yield
    monkeypatch.setattr(config, "_loaded", False)


def write_env(tmp_path, body: str):
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_values_into_environ(tmp_path, monkeypatch):
    env = write_env(tmp_path, "KEEPA_API_KEY=realkey123\nKEEPA_BUCKET_MAX=300\n")
    config.load_env(env, force=True)
    assert os.environ["KEEPA_API_KEY"] == "realkey123"
    assert config.get("KEEPA_BUCKET_MAX") == "300"


def test_real_environment_variable_wins_over_dotenv(tmp_path, monkeypatch):
    """systemd EnvironmentFile on the server must override a stale local .env."""
    monkeypatch.setenv("KEEPA_API_KEY", "from_environment")
    env = write_env(tmp_path, "KEEPA_API_KEY=from_dotenv\n")
    config.load_env(env, force=True)
    assert os.environ["KEEPA_API_KEY"] == "from_environment"


def test_comments_blanks_and_quotes_are_handled(tmp_path):
    env = write_env(
        tmp_path,
        "# a comment\n\nKEEPA_API_KEY=\"quoted_key\"\n  KEEPA_DOMAIN = 2 \n",
    )
    config.load_env(env, force=True)
    assert os.environ["KEEPA_API_KEY"] == "quoted_key"
    assert os.environ["KEEPA_DOMAIN"] == "2"


def test_unedited_placeholder_counts_as_missing(tmp_path):
    """The whole point: `your_key_here` must not be sent to Keepa as a key."""
    env = write_env(tmp_path, "KEEPA_API_KEY=your_key_here\n")
    config.load_env(env, force=True)
    assert config.get("KEEPA_API_KEY") is None
    with pytest.raises(config.MissingApiKey):
        config.require_api_key()


def test_missing_key_error_explains_both_options(tmp_path):
    config.load_env(write_env(tmp_path, ""), force=True)
    with pytest.raises(config.MissingApiKey) as exc:
        config.require_api_key()
    message = str(exc.value)
    assert ".env" in message
    assert "setx" in message
    assert "keepa.com" in message


def test_absent_dotenv_is_not_an_error(tmp_path):
    assert config.load_env(tmp_path / "nope.env", force=True) == {}


def test_client_raises_the_helpful_error(tmp_path, monkeypatch):
    from core.cache import Cache
    from core.client import KeepaClient

    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "absent.env")
    cache = Cache(tmp_path / "t.db")
    with pytest.raises(config.MissingApiKey, match="Edit"):
        KeepaClient(cache=cache)
    cache.close()
