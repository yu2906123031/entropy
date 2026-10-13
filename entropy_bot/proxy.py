"""Validated outbound HTTP CONNECT proxy configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit, urlunsplit

from entropy_bot.errors import ConfigError


@dataclass(frozen=True)
class ProxyConfig:
    url: str = field(repr=False)
    host: str
    port: int
    auth: tuple[str, str] | None = field(repr=False)

    @property
    def redacted_url(self) -> str:
        parsed = urlsplit(self.url)
        userinfo = "***:***@" if self.auth is not None else ""
        host = f"[{parsed.hostname}]" if ":" in self.host else self.host
        return urlunsplit((parsed.scheme, f"{userinfo}{host}:{self.port}", "", "", ""))


def parse_proxy_url(value: str) -> ProxyConfig:
    """Parse an explicit HTTP proxy used for HTTPS/WSS CONNECT tunnels."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ConfigError("OUTBOUND_PROXY_URL is invalid") from exc
    if (
        parsed.scheme.lower() != "http"
        or not parsed.hostname
        or port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("OUTBOUND_PROXY_URL must be an http://host:port CONNECT proxy URL")
    if (parsed.username is None) != (parsed.password is None):
        raise ConfigError("OUTBOUND_PROXY_URL credentials require both username and password")
    auth = None
    if parsed.username is not None:
        auth = (unquote(parsed.username), unquote(parsed.password or ""))
        if not all(auth):
            raise ConfigError("OUTBOUND_PROXY_URL credentials must be non-empty")
    return ProxyConfig(value, parsed.hostname, port, auth)


def redact_proxy_url(value: str | None) -> str | None:
    return parse_proxy_url(value).redacted_url if value else None