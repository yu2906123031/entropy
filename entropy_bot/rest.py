"""Official Hyperliquid HTTP info client. Be polite: cache + small pauses."""

from __future__ import annotations

import logging
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from entropy_bot.coins import ALLOWED_DEX, assert_no_foreign_venue, assert_tradable
from entropy_bot.errors import RateLimited, RequestWeightLimited, error_text, is_weight_limit_error
from entropy_bot.proxy import parse_proxy_url

log = logging.getLogger("entropy_bot.rest")

DEFAULT_TIMEOUT = 15.0
INFO_PAUSE_S = 0.5
INFO_COOLDOWN_BASE_S = 15.0
INFO_COOLDOWN_MAX_S = 120.0


class InfoClient:
    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT,
                 info_min_interval_s: float = INFO_PAUSE_S,
                 cooldown_base_s: float = INFO_COOLDOWN_BASE_S,
                 cooldown_max_s: float = INFO_COOLDOWN_MAX_S,
                 proxy_url: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False
        self._uses_proxy = proxy_url is not None
        if proxy_url:
            proxy = parse_proxy_url(proxy_url)
            self.session.proxies.update({"http": proxy.url, "https": proxy.url})
        self._perp_dexs: list[Any] | None = None
        self._meta: dict[str, Any] | None = None
        self._meta_and_ctxs: Any | None = None
        self.info_min_interval_s = max(0.0, info_min_interval_s)
        self.cooldown_base_s = max(1.0, cooldown_base_s)
        self.cooldown_max_s = max(self.cooldown_base_s, cooldown_max_s)
        self._info_lock = threading.Lock()
        self._last_info_at = 0.0
        self._info_cooldown_until = 0.0
        self._info_backoff_s = 0.0
        self._info_429_logged = False

    def info_cooldown_remaining(self) -> float:
        return max(0.0, self._info_cooldown_until - time.monotonic())

    def info_cooldown_active(self) -> bool:
        return self.info_cooldown_remaining() > 0

    def _retry_after_s(self, response: requests.Response) -> float | None:
        raw = response.headers.get("Retry-After", "").strip()
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(raw).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                return None

    def close(self) -> None:
        self.session.close()

    def post_info(
        self,
        payload: dict[str, Any],
        *,
        cacheable: bool = False,
        optional: bool = False,
    ) -> Any:
        assert_no_foreign_venue(payload)
        url = f"{self.base_url}/info"
        with self._info_lock:
            remaining = self.info_cooldown_remaining()
            if remaining > 0:
                if optional:
                    return None
                raise RateLimited(f"info cooldown active for {remaining:.1f}s")
            wait = self.info_min_interval_s - (time.monotonic() - self._last_info_at)
            if wait > 0:
                time.sleep(wait)
            try:
                response = self.session.post(
                    url, json=payload, timeout=self.timeout, allow_redirects=False
                )
                if response.status_code == 429:
                    retry_after = self._retry_after_s(response)
                    self._info_backoff_s = min(
                        self.cooldown_max_s,
                        retry_after if retry_after is not None else
                        (self._info_backoff_s * 2 if self._info_backoff_s else self.cooldown_base_s),
                    )
                    self._info_cooldown_until = time.monotonic() + self._info_backoff_s
                    if not self._info_429_logged:
                        log.warning("info 429 for %s; shared cooldown %.1fs",
                                    payload.get("type"), self._info_backoff_s)
                        self._info_429_logged = True
            except requests.RequestException as exc:
                if optional:
                    if self._uses_proxy:
                        log.warning("info request via configured proxy failed: %s", type(exc).__name__)
                    else:
                        log.warning("info request failed: %s", exc)
                    return None
                if self._uses_proxy:
                    raise requests.RequestException("info request via configured proxy failed") from exc
                raise
            finally:
                self._last_info_at = time.monotonic()
        if 300 <= response.status_code < 400 or response.url != url:
            raise requests.RequestException("info endpoint redirect or URL mismatch")
        if response.status_code == 429:
            if optional:
                return None
            raise RateLimited(f"info 429 for {payload.get('type')}")
        if optional and not response.ok:
            log.warning("info HTTP %s for %s", response.status_code, payload.get("type"))
            return None
        if not optional:
            response.raise_for_status()
        if not response.content or response.content.strip() in {b"", b"null"}:
            return None
        data = response.json()
        self._info_backoff_s = 0.0
        self._info_429_logged = False
        return data

    def perp_dexs(self, *, refresh: bool = False) -> list[Any]:
        if self._perp_dexs is None or refresh:
            self._perp_dexs = self.post_info({"type": "perpDexs"}, cacheable=True)
        return self._perp_dexs

    def meta(self, dex: str = ALLOWED_DEX, *, refresh: bool = False) -> dict[str, Any]:
        if dex != ALLOWED_DEX:
            raise ValueError(f"this client only queries dex={ALLOWED_DEX!r}")
        if self._meta is None or refresh:
            self._meta = self.post_info({"type": "meta", "dex": dex}, cacheable=True)
        return self._meta

    def meta_and_asset_ctxs(self, dex: str = ALLOWED_DEX, *, refresh: bool = False) -> Any:
        if dex != ALLOWED_DEX:
            raise ValueError(f"this client only queries dex={ALLOWED_DEX!r}")
        if self._meta_and_ctxs is None or refresh:
            self._meta_and_ctxs = self.post_info(
                {"type": "metaAndAssetCtxs", "dex": dex}, cacheable=True
            )
            if isinstance(self._meta_and_ctxs, list) and self._meta_and_ctxs:
                self._meta = self._meta_and_ctxs[0]
        return self._meta_and_ctxs

    def l2_book(self, coin: str) -> dict[str, Any]:
        coin = assert_tradable(coin)
        return self.post_info({"type": "l2Book", "coin": coin})

    def clearinghouse_state(
        self,
        user: str,
        dex: str = ALLOWED_DEX,
        *,
        optional: bool = False,
    ) -> dict[str, Any] | None:
        if dex != ALLOWED_DEX:
            raise ValueError(f"this client only queries dex={ALLOWED_DEX!r}")
        return self.post_info(
            {"type": "clearinghouseState", "user": user, "dex": dex},
            optional=optional,
        )

    def open_orders(
        self,
        user: str,
        dex: str = ALLOWED_DEX,
        *,
        optional: bool = False,
    ) -> list[dict[str, Any]] | None:
        """frontendOpenOrders with dex:io. null/429 returns None — callers must keep cache."""
        if dex != ALLOWED_DEX:
            raise ValueError(f"this client only queries dex={ALLOWED_DEX!r}")
        data = self.post_info(
            {"type": "frontendOpenOrders", "user": user, "dex": dex},
            optional=optional,
        )
        if data is None:
            return None
        if not isinstance(data, list):
            if optional:
                log.warning("frontendOpenOrders returned non-list; treating as null")
                return None
            raise ValueError("frontendOpenOrders expected a list")
        return data

    def user_fills(self, user: str, *, optional: bool = True) -> list[dict[str, Any]] | None:
        """Recent fills for the master account. Observe-only; 429/null → None."""
        data = self.post_info({"type": "userFills", "user": user}, optional=optional)
        if data is None:
            return None
        if not isinstance(data, list):
            if optional:
                log.warning("userFills returned non-list; treating as null")
                return None
            raise ValueError("userFills expected a list")
        return [row for row in data if isinstance(row, dict)]

    def order_status(self, user: str, oid_or_cloid: str | int, *, optional: bool = True) -> Any:
        """Official orderStatus lookup; CLOIDs are accepted in the `oid` field."""
        return self.post_info(
            {"type": "orderStatus", "user": user, "oid": oid_or_cloid}, optional=optional
        )

    def extra_agents(self, user: str, *, optional: bool = True) -> list[Any] | None:
        data = self.post_info({"type": "extraAgents", "user": user}, optional=optional, cacheable=True)
        if data is None:
            return None
        return data if isinstance(data, list) else None

    def post_exchange(self, payload: dict[str, Any]) -> Any:
        assert_no_foreign_venue(payload)
        url = f"{self.base_url}/exchange"
        try:
            response = self.session.post(
                url, json=payload, timeout=self.timeout, allow_redirects=False
            )
        except requests.RequestException as exc:
            detail = "configured proxy transport failure" if self._uses_proxy else str(exc)
            raise RateLimited(f"exchange request failed: {detail}") from exc
        if 300 <= response.status_code < 400 or response.url != url:
            raise RateLimited("exchange endpoint redirect or URL mismatch")
        if response.status_code == 429:
            text = response.text or "exchange 429"
            if is_weight_limit_error(text):
                raise RequestWeightLimited(text)
            raise RateLimited("exchange 429")
        if not response.ok and is_weight_limit_error(response.text):
            raise RequestWeightLimited(response.text)
        response.raise_for_status()
        if not response.content or response.content.strip() in {b"", b"null"}:
            return None
        data = response.json()
        if is_weight_limit_error(data):
            raise RequestWeightLimited(error_text(data))
        return data
