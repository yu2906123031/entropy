import os

import pytest

from entropy_bot.config import load_settings
from entropy_bot.errors import ConfigError
from entropy_bot.proxy import parse_proxy_url, redact_proxy_url
from entropy_bot.rest import InfoClient
from entropy_bot.ws import BookFeed


def _clean(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(("HYPERLIQUID_", "ENTROPY_", "OUTBOUND_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ENTROPY_INTENT_DB", str(tmp_path / "intents.sqlite3"))


def test_proxy_parse_and_redaction_hides_credentials():
    raw = "http://alice:p%40ss@proxy.example:8080"
    proxy = parse_proxy_url(raw)
    assert (proxy.host, proxy.port, proxy.auth) == ("proxy.example", 8080, ("alice", "p@ss"))
    assert redact_proxy_url(raw) == "http://***:***@proxy.example:8080"
    assert "alice" not in proxy.redacted_url and "p%40ss" not in proxy.redacted_url


@pytest.mark.parametrize("raw", [
    "https://proxy.example:8080", "socks5://proxy.example:1080",
    "http://proxy.example", "http://user@proxy.example:8080",
    "http://proxy.example:8080/path", "http://proxy.example:99999",
])
def test_proxy_validation_rejects_unsupported_urls(raw):
    with pytest.raises(ConfigError, match="OUTBOUND_PROXY_URL"):
        parse_proxy_url(raw)


def test_settings_validates_proxy_and_repr_redacts(monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    raw = "http://alice:secret@127.0.0.1:3128"
    monkeypatch.setenv("OUTBOUND_PROXY_URL", raw)
    settings = load_settings(load_private_key=False)
    assert settings.outbound_proxy_url == raw
    assert "secret" not in repr(settings)


def test_settings_loads_proxy_from_credential_file(monkeypatch, tmp_path):
    proxy_file = tmp_path / "proxy"
    proxy_file.write_text("http://user:pass@127.0.0.1:8080\n")
    monkeypatch.delenv("OUTBOUND_PROXY_URL", raising=False)
    monkeypatch.setenv("OUTBOUND_PROXY_FILE", str(proxy_file))
    monkeypatch.setenv("ENTROPY_INTENT_DB", str(tmp_path / "intents.db"))
    settings = load_settings(load_private_key=False)
    assert settings.outbound_proxy_url == "http://user:pass@127.0.0.1:8080"
    assert "secret" not in repr(settings)


def test_http_and_websocket_clients_receive_same_explicit_proxy():
    raw = "http://alice:secret@127.0.0.1:3128"
    client = InfoClient("https://api.hyperliquid.xyz", proxy_url=raw)
    try:
        assert client.session.trust_env is False
        assert client.session.proxies == {"http": raw, "https": raw}
    finally:
        client.close()
    feed = BookFeed("wss://api.hyperliquid.xyz/ws", ("io:ANTH",), lambda *_: None,
                    proxy_url=raw)
    assert feed.proxy is not None
    assert (feed.proxy.host, feed.proxy.port, feed.proxy.auth) == (
        "127.0.0.1", 3128, ("alice", "secret"))


def test_proxy_failures_do_not_log_credentials(caplog):
    feed = BookFeed("wss://api.hyperliquid.xyz/ws", ("io:ANTH",), lambda *_: None,
                    proxy_url="http://alice:secret@127.0.0.1:3128")
    feed._on_error(None, RuntimeError("http://alice:secret@127.0.0.1:3128 failed"))
    assert "alice" not in caplog.text and "secret" not in caplog.text


def test_account2_units_are_isolated_from_account1():
    live = open("deploy/entropy-account2-live.service", encoding="utf-8").read()
    guardian = open("deploy/entropy-account2-guardian.service", encoding="utf-8").read()
    combined = live + guardian
    assert "entropy-account2-guardian.service" in live
    assert "QUOTE_NOTIONAL_MIN_USD=50" in live and "QUOTE_NOTIONAL_MAX_USD=100" in live
    assert "MAX_LEVERAGE=5" in live and "COINS=io:ANTH" in live
    assert "/run/entropy-bot-account2" in combined
    assert "/var/lib/entropy-bot-account2" in combined
    assert "entropy-account2-grid.cred" in combined
    assert "account2-agent-keystore.json" in combined
    assert "/run/entropy-bot/" not in combined and "/var/lib/entropy-bot/" not in combined