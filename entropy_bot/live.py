"""Official-API live MM. Port of entropy-desk.user.js 1.6.1.

Talks only to https://api.hyperliquid.xyz and wss://api.hyperliquid.xyz/ws.
No Tampermonkey / window.ethereum. Per coin: at most 1 buy + 1 sell.

Dead-man `scheduleCancel` is **account-wide**: when it fires it cancels every
open order on the master account, not just io:ANTH / io:SNDK.
"""

from __future__ import annotations

import logging
import math
import random
import signal
import threading
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from entropy_bot.coins import ALLOWED_DEX, Market, perp_dex_info
from entropy_bot.config import Settings, live_notional, require_live
from entropy_bot.fees import (
    ALO_TIF_NOTE,
    alo_fill_rebate_usd,
    describe_fee_model,
    estimate_from_settings,
    fee_for_notional,
)
from entropy_bot.orders import (
    LiveSigner,
    action_ok,
    action_statuses_ok,
    as_oid,
    format_fee_banner,
    is_alo_reject,
    is_bot_cloid,
    resp_statuses,
    status_error,
    status_oid,
)
from entropy_bot.quoting import (
    BookTop,
    QuotePlan,
    book_top,
    quote_plan,
    sync_pos_since,
)
from entropy_bot.diagnostics import BookSnap, FillDiagnostics, snap_from_top
from entropy_bot.errors import LiveGuardError, RateLimited, RequestWeightLimited, is_weight_limit_error
from entropy_bot.rest import InfoClient
from entropy_bot.intents import IntentWAL
from entropy_bot.status import load_io_markets
from entropy_bot.ws import BookFeed
from entropy_bot.safety import AccountLock, atomic_json, fresh_guardian

log = logging.getLogger("entropy_bot.live")

WS_STALE_S = 15.0
LOOP_S = 0.4
DEADMAN_AHEAD_MS = 20_000
DEADMAN_MIN_MS = 5_000
DEADMAN_FALLBACK_MS = 6_000
DEADMAN_REMAIN_LT_S = 8.0
DEADMAN_REFRESH_S = DEADMAN_REMAIN_LT_S  # alias: refresh only when remaining < 8s
IOC_GAP_S = 10.0
WEIGHT_BACKOFF_FLOOR_S = 30.0


@dataclass
class RestSlot:
    oid: int | None = None
    cloid: str | None = None
    px: float | None = None
    sz: float | None = None
    tif: str = "Alo"
    reduce_only: bool = False
    placed_at: float = 0.0

    def occupied(self) -> bool:
        return self.oid is not None or self.cloid is not None


def empty_rests(coins: tuple[str, ...]) -> dict[str, dict[str, RestSlot]]:
    return {coin: {"B": RestSlot(), "A": RestSlot()} for coin in coins}


def order_side_buy(order: dict[str, Any]) -> bool | None:
    side = order.get("side")
    if side in {"B", "b"}:
        return True
    if side in {"A", "S", "a", "s"}:
        return False
    if order.get("b") is True:
        return True
    if order.get("b") is False:
        return False
    return None


def match_order_coin(order: dict[str, Any], markets: dict[str, Market]) -> str | None:
    raw = str(order.get("coin") or "")
    if raw.startswith(("xyz:", "vntl:")):
        return None
    if raw in markets:
        return raw
    aid_raw = order.get("a", order.get("asset"))
    aid: int | None
    try:
        aid = int(aid_raw) if aid_raw is not None else None
    except (TypeError, ValueError):
        aid = None
    for coin, market in markets.items():
        base = coin.split(":", 1)[1]
        if raw == base or raw == f"@{market.asset_id}" or raw == str(market.asset_id):
            return coin
        if aid is not None and aid == market.asset_id:
            return coin
    return None


def apply_open_orders(
    rests: dict[str, dict[str, RestSlot]],
    extras: dict[str, list[int]],
    opens: list[dict[str, Any]] | None,
    markets: dict[str, Market],
) -> tuple[dict[str, dict[str, RestSlot]], dict[str, list[int]]]:
    """Rebuild rest cache from frontendOpenOrders. None/429 must not wipe cache."""
    if opens is None:
        log.warning("open-order query null/429; keeping local rest cache")
        return rests, extras
    next_rests = empty_rests(tuple(markets))
    next_extras: dict[str, list[int]] = {coin: [] for coin in markets}
    for order in opens:
        coin = match_order_coin(order, markets)
        if coin is None or coin not in next_rests:
            continue
        oid_raw = order.get("oid", order.get("o"))
        if oid_raw is None:
            continue
        try:
            oid = as_oid(oid_raw)
        except ValueError:
            continue
        is_buy = order_side_buy(order)
        side = "B" if is_buy is True else "A" if is_buy is False else None
        if side is None:
            continue
        slot = next_rests[coin][side]
        cloid = order.get("cloid") or order.get("cloidStr")
        if not slot.occupied():
            slot.oid = oid
            slot.cloid = cloid if isinstance(cloid, str) else None
            try:
                slot.px = float(order.get("limitPx") or order.get("px") or 0) or None
            except (TypeError, ValueError):
                slot.px = None
        else:
            next_extras[coin].append(oid)
    for coin, extra_oids in next_extras.items():
        if extra_oids:
            log.info("extra rests %s oids=%s", coin, extra_oids)
    return next_rests, next_extras


def list_cached_oids(
    rests: dict[str, dict[str, RestSlot]],
    extras: dict[str, list[int]],
    coin: str,
) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for side in ("B", "A"):
        slot = rests.get(coin, {}).get(side)
        if slot and slot.oid is not None and slot.oid not in seen:
            seen.add(slot.oid)
            out.append(slot.oid)
    for oid in extras.get(coin, []):
        if oid not in seen:
            seen.add(oid)
            out.append(oid)
    return out


def list_cached_cloids(rests: dict[str, dict[str, RestSlot]], coin: str) -> list[str]:
    out: list[str] = []
    for side in ("B", "A"):
        slot = rests.get(coin, {}).get(side)
        if slot and slot.cloid:
            out.append(slot.cloid)
    return out


def clear_coin_rests(rests: dict[str, dict[str, RestSlot]], coin: str) -> None:
    rests[coin] = {"B": RestSlot(), "A": RestSlot()}


def positions_from_state(
    state: dict[str, Any] | None,
    coins: tuple[str, ...],
    markets: dict[str, Market],
) -> dict[str, float] | None:
    if state is None:
        return None
    out = {coin: 0.0 for coin in coins}
    rows = state.get("assetPositions")
    if not isinstance(rows, list):
        return None
    for item in rows:
        pos = (item or {}).get("position") or {}
        coin = match_order_coin({"coin": pos.get("coin"), "a": None}, markets)
        if coin is None or coin not in out:
            raw = str(pos.get("coin") or "")
            if raw in out:
                coin = raw
            else:
                continue
        try:
            value = float(pos.get("szi") or 0)
            if not math.isfinite(value):
                return None
            out[coin] = value
        except (TypeError, ValueError):
            return None
    return out


def entry_prices_from_state(
    state: dict[str, Any] | None,
    coins: tuple[str, ...],
    markets: dict[str, Market],
) -> dict[str, float] | None:
    if state is None or not isinstance(state.get("assetPositions"), list):
        return None
    out = {coin: 0.0 for coin in coins}
    for item in state["assetPositions"]:
        pos = (item or {}).get("position") or {}
        coin = match_order_coin({"coin": pos.get("coin"), "a": None}, markets)
        if coin not in out:
            continue
        try:
            value = float(pos.get("entryPx") or 0)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value < 0:
            return None
        out[coin] = value
    return out


def deadman_deadline_ms(ahead_ms: int, *, now_ms: int | None = None) -> int:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    t = now + int(ahead_ms)
    if t - now < DEADMAN_MIN_MS:
        t = now + DEADMAN_FALLBACK_MS
    return t


def weight_backoff_s(min_replace_s: float, floor_s: float = WEIGHT_BACKOFF_FLOOR_S) -> float:
    """Signed-write pause after a cumulative request-weight error. Waiting does not grow the cap."""
    return max(float(min_replace_s), float(floor_s))


def replace_allowed(
    *,
    take: bool,
    has_rest: bool,
    elapsed_s: float,
    min_replace_s: float,
) -> bool:
    """Throttle maker cancel+replace. Reduce-only IOC exits remain available."""
    if take:
        return True
    if not has_rest:
        return True
    return elapsed_s >= min_replace_s


def deadman_needs_refresh(
    deadline_s: float,
    now_s: float,
    remain_lt_s: float = DEADMAN_REMAIN_LT_S,
) -> bool:
    """Renew scheduleCancel only when unset or remaining time is under 8s."""
    if deadline_s <= 0:
        return True
    return (deadline_s - now_s) < remain_lt_s


def side_occupied(rests: dict[str, dict[str, RestSlot]], coin: str, side: str) -> bool:
    slot = rests.get(coin, {}).get(side)
    return bool(slot and slot.occupied())


@dataclass
class LiveQuoter:
    settings: Settings
    markets: dict[str, Market]
    client: InfoClient
    signer: LiveSigner
    notional: float = 0.0
    rests: dict[str, dict[str, RestSlot]] = field(default_factory=dict)
    extra_oids: dict[str, list[int]] = field(default_factory=dict)
    pos: dict[str, float] = field(default_factory=dict)
    entry_px: dict[str, float] = field(default_factory=dict)
    pos_since: dict[str, float] = field(default_factory=dict)
    last_plan_key: dict[str, str] = field(default_factory=dict)
    quote_placed_at: dict[str, float] = field(default_factory=dict)
    last_take_at: dict[str, float] = field(default_factory=dict)
    last_pos_poll: float = 0.0
    last_fill_poll: float = 0.0
    last_order_poll: float = 0.0
    last_deadman: float = 0.0
    last_replace: dict[str, float] = field(default_factory=dict)
    deadman_until: float = 0.0
    _weight_backoff_until: float = 0.0
    _weight_logged: bool = False
    _replace_skip_logged: dict[str, bool] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    books: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)
    diag: FillDiagnostics | None = None
    _feed: BookFeed | None = None
    intent_wal: IntentWAL | None = None
    open_orders_known: bool = True
    position_known: bool = False
    external_orders: list[dict[str, Any]] = field(default_factory=list)
    leverage_proof: dict[str, dict[str, Any]] = field(default_factory=dict)
    leverage_blocked: bool = False
    _deadman_retry_at: float = 0.0
    _deadman_unavailable_logged: bool = False
    _uncertain_logged: bool = False
    _last_safety_cancel: float = 0.0
    _fill_reconcile_pending: bool = False
    _notional_rng: random.Random = field(default_factory=random.SystemRandom, repr=False)

    def __post_init__(self) -> None:
        if not self.notional:
            self.notional = live_notional(self.settings)
        coins = tuple(self.markets)
        if not self.rests:
            self.rests = empty_rests(coins)
        if not self.extra_oids:
            self.extra_oids = {coin: [] for coin in coins}
        if not self.pos:
            self.pos = {coin: 0.0 for coin in coins}
        if not self.entry_px:
            self.entry_px = {coin: 0.0 for coin in coins}
        if not self.pos_since:
            self.pos_since = {coin: 0.0 for coin in coins}
        if not self.last_replace:
            self.last_replace = {coin: 0.0 for coin in coins}
        if not self._replace_skip_logged:
            self._replace_skip_logged = {coin: False for coin in coins}
        if self.diag is None:
            self.diag = FillDiagnostics(coins)

    def _sample_opening_notional(self) -> None:
        low = getattr(self.settings, "quote_notional_min_usd", None)
        high = getattr(self.settings, "quote_notional_max_usd", None)
        low = live_notional(self.settings) if low is None else float(low)
        high = low if high is None else float(high)
        self.notional = round(self._notional_rng.uniform(low, high), 2)

    def _in_weight_backoff(self) -> bool:
        return time.time() < self._weight_backoff_until

    def _trip_weight_backoff(self, err: object) -> None:
        wait = weight_backoff_s(self.settings.min_replace_s)
        self._weight_backoff_until = time.time() + wait
        if not self._weight_logged:
            log.warning(
                "request weight exhausted; backing off signed writes for %.0fs "
                "(waiting does not restore the cap): %s",
                wait,
                str(err).replace("\r", "\\r").replace("\n", "\\n"),
            )
            self._weight_logged = True

    def _mark_replaced(self, coin: str) -> None:
        self.last_replace[coin] = time.time()
        self._replace_skip_logged[coin] = False

    def _has_rest(self, coin: str) -> bool:
        return side_occupied(self.rests, coin, "B") or side_occupied(self.rests, coin, "A")

    def on_book(self, coin: str, data: dict[str, Any]) -> None:
        with self._lock:
            self.books[coin] = (time.time(), data)

    def peek_book(self, coin: str) -> BookSnap | None:
        """Cache/WS l2Book only. Diagnostics must not open a WS or REST-fallback."""
        now = time.time()
        ts = 0.0
        data: dict[str, Any] | None = None
        with self._lock:
            if coin in self.books:
                ts, data = self.books[coin]
        feed = self._feed
        if feed is not None:
            latest = feed.latest(coin)
            if latest is not None:
                ts, data = latest
        if data is None:
            return None
        return snap_from_top(book_top(coin, data), now - ts)

    def _diag_quotes(self, coin: str, n: int, *, take: bool) -> None:
        if take or n <= 0 or self.diag is None:
            return
        try:
            self.diag.note_quotes(coin, n)
        except Exception as exc:  # noqa: BLE001
            log.warning("fill-diag quotes: %s", exc)

    def on_user_fills(self, data: dict[str, Any]) -> None:
        fills = data.get("fills") if isinstance(data, dict) else None
        if not isinstance(fills, list):
            return
        if fills and not data.get("isSnapshot"):
            # A maker fill can leave the opposite opening quote resting. Force an
            # authoritative position read on the next loop before any new quote.
            self._fill_reconcile_pending = True
            self.position_known = False
        if self.diag is None:
            return
        try:
            self.diag.ingest_fills(fills, self.peek_book, prime=bool(data.get("isSnapshot")))
        except Exception as exc:  # noqa: BLE001
            log.warning("fill-diag userFills: %s", exc)

    def poll_user_fills(self, *, force: bool = False) -> None:
        if self.diag is None:
            return
        now = time.time()
        if not force and now - self.last_fill_poll < getattr(self.settings, "fill_reconcile_s", 60.0):
            return
        rows = self.client.user_fills(self.signer.account, optional=True)
        self.last_fill_poll = now
        if rows is None:
            return
        try:
            self.diag.ingest_fills(rows, self.peek_book, prime=not self.diag.primed)
        except Exception as exc:  # noqa: BLE001
            log.warning("fill-diag userFills poll: %s", exc)

    def flush_markouts(self) -> None:
        if self.diag is None:
            return
        try:
            self.diag.flush_markouts(self.peek_book)
        except Exception as exc:  # noqa: BLE001
            log.warning("fill-diag markout: %s", exc)

    def bootstrap_isolated(self) -> None:
        proofs: dict[str, dict[str, Any]] = {}
        for market in self.markets.values():
            fee = estimate_from_settings(market, self.settings)
            maker_usd = fee_for_notional(fee, self.notional, maker=True)
            rebate_usd = (
                alo_fill_rebate_usd(self.notional, self.settings.maker_rebate_bps)
                if self.settings.maker_rebate_bps is not None
                else None
            )
            print(
                format_fee_banner(market.coin, fee.summary(), self.notional, maker_usd, rebate_usd),
                flush=True,
            )
            payload = self.signer.signed_update_leverage(market, self.settings.max_leverage)
            resp = self.client.post_exchange(payload)
            log.info("isolated leverage %s -> %s", market.coin, resp)
            if not action_ok(resp):
                raise LiveGuardError(f"leverage update rejected for {market.coin}")
            proofs[market.coin] = {
                "account": self.signer.account,
                "coin": market.coin,
                "value": min(self.settings.max_leverage, market.max_leverage),
                "response_timestamp": time.time(),
                "response": resp,
            }
        self.leverage_proof = proofs
        atomic_json(Path(self.settings.state_dir) / "leverage-proof.json", {
            "account": self.signer.account, "dex": ALLOWED_DEX, "proofs": proofs,
        })
        state = self.client.clearinghouse_state(self.signer.account, ALLOWED_DEX, optional=True)
        if not self._isolated_leverage_verified(state, allow_flat_proof=True):
            raise LiveGuardError("isolated leverage verification failed")

    def _isolated_leverage_verified(self, state: Any, *, allow_flat_proof: bool = False) -> bool:
        if not isinstance(state, dict):
            return False
        found: dict[str, tuple[str, int]] = {}
        for row in state.get("assetPositions") or []:
            pos = row.get("position") if isinstance(row, dict) else None
            if not isinstance(pos, dict) or pos.get("coin") not in self.markets:
                continue
            lev = pos.get("leverage")
            if not isinstance(lev, dict):
                return False
            try:
                value = lev.get("value")
                if value is None:
                    return False
                found[str(pos["coin"])] = (str(lev.get("type", "")).lower(), int(value))
            except (TypeError, ValueError):
                return False
        if not found:
            if allow_flat_proof:
                log.warning("flat account: persisting exchange-accepted updateLeverage response proof")
                return all(coin in self.leverage_proof for coin in self.markets)
            return False
        return all(
            found[coin] == ("isolated", min(self.settings.max_leverage, market.max_leverage))
            for coin, market in self.markets.items()
            if coin in found
        )

    def _fee_banner(self, coin: str) -> None:
        market = self.markets[coin]
        fee = estimate_from_settings(market, self.settings)
        maker_usd = fee_for_notional(fee, self.notional, maker=True)
        rebate_usd = (
            alo_fill_rebate_usd(self.notional, self.settings.maker_rebate_bps)
            if self.settings.maker_rebate_bps is not None
            else None
        )
        print(format_fee_banner(coin, fee.summary(), self.notional, maker_usd, rebate_usd), flush=True)

    def refresh_positions(self, *, force: bool = False) -> bool:
        now = time.time()
        if not force and now - self.last_pos_poll < getattr(self.settings, "position_reconcile_s", 10.0):
            return self.position_known
        state = self.client.clearinghouse_state(self.signer.account, ALLOWED_DEX, optional=True)
        parsed = positions_from_state(state, tuple(self.markets), self.markets)
        entries = entry_prices_from_state(state, tuple(self.markets), self.markets)
        if parsed is None or entries is None:
            log.warning("clearinghouseState null/429; keeping inventory cache")
            self.last_pos_poll = now
            self.position_known = False
            return False
        self.pos_since = sync_pos_since(self.pos, parsed, self.pos_since, now)
        self.pos = parsed
        self.entry_px = entries
        self.last_pos_poll = now
        self.position_known = True
        if any(abs(value) > 0 for value in parsed.values()) and not self._isolated_leverage_verified(state):
            self.leverage_blocked = True
            log.critical("authoritative position leverage mismatch; opening quotes blocked")
        return True

    def enforce_position_leverage(self) -> None:
        """Flatten immediately and terminate the session after authoritative mismatch."""
        if not self.leverage_blocked:
            return
        for coin, size in self.pos.items():
            if abs(size) <= 0:
                continue
            self.cancel_coin_rests(refetch=True, only_coin=coin)
            top = self.book_for(coin, self._feed)
            if top is None:
                continue
            self.pos_since[coin] = time.time() - getattr(self.settings, "flatten_take_s", 120.0) - 1.0
            plan = self.plan_for(coin, top)
            if plan.take:
                self._place(coin, plan)
        raise LiveGuardError("position leverage mismatch; emergency flatten attempted")

    def guardian_ready(self) -> bool:
        return fresh_guardian(self.settings.guardian_ready_path, self.signer.account,
                              max_age=self.settings.guardian_max_age,
                              token_path=self.settings.guardian_token_path)

    def refresh_lease(self) -> None:
        atomic_json(self.settings.guardian_lease_path,
                    {"heartbeat": time.time(), "account": self.signer.account, "dex": ALLOWED_DEX})

    def reconcile_orders(self, *, force: bool = False) -> bool:
        now = time.time()
        if not force and now - self.last_order_poll < getattr(self.settings, "open_orders_reconcile_s", 15.0):
            return self.open_orders_known and not self.external_orders
        opens = self.client.open_orders(self.signer.account, ALLOWED_DEX, optional=True)
        self.last_order_poll = now
        if opens is None:
            self.open_orders_known = False
            return False
        self.external_orders = [o for o in opens if not is_bot_cloid(o.get("cloid") or o.get("cloidStr"))]
        bot_opens = [o for o in opens if is_bot_cloid(o.get("cloid") or o.get("cloidStr"))]
        if self.intent_wal is not None:
            unresolved = self.intent_wal.reconcile_open_orders(bot_opens)
            if unresolved:
                fills = self.client.user_fills(self.signer.account, optional=True)
                if fills is None:
                    self.open_orders_known = False
                    return False
                unresolved = self.intent_wal.reconcile_history(
                    unresolved, fills,
                    lambda cloid: self.client.order_status(self.signer.account, cloid, optional=True),
                )
                if unresolved:
                    self.open_orders_known = False
                    return False
        self.rests, self.extra_oids = apply_open_orders(self.rests, self.extra_oids, bot_opens, self.markets)
        self.open_orders_known = True
        return not self.external_orders

    def book_for(self, coin: str, feed: BookFeed | None) -> BookTop | None:
        now = time.time()
        ts = 0.0
        data: dict[str, Any] | None = None
        with self._lock:
            if coin in self.books:
                ts, data = self.books[coin]
        if feed is not None:
            latest = feed.latest(coin)
            if latest is not None:
                ts, data = latest
        stale = data is None or (now - ts) > WS_STALE_S
        if stale:
            try:
                data = self.client.l2_book(coin)
                ts = time.time()
                with self._lock:
                    self.books[coin] = (ts, data)
            except Exception as exc:  # noqa: BLE001
                log.warning("REST l2Book fallback failed %s: %s", coin, exc)
                return None
        raw_exchange_ms = (data or {}).get("time")
        try:
            exchange_ms = int(raw_exchange_ms) if raw_exchange_ms is not None else 0
        except (TypeError, ValueError):
            exchange_ms = 0
        age_ms = time.time() * 1000 - exchange_ms
        if exchange_ms <= 1_000_000_000_000 or age_ms > WS_STALE_S * 1000 or age_ms < -5000:
            log.warning("invalid/stale exchange l2Book timestamp %s", coin)
            return None
        return book_top(coin, data or {})

    def plan_for(self, coin: str, top: BookTop) -> QuotePlan:
        now = time.time()
        szi = self.pos.get(coin, 0.0)
        since = self.pos_since.get(coin, 0.0)
        age_ms = int(max(0.0, (now - since) * 1000)) if since and abs(szi) > 0 else 0
        return quote_plan(
            self.markets[coin],
            top,
            szi=szi,
            age_ms=age_ms,
            notional_usd=self.notional,
            flatten_far_ms=int(getattr(self.settings, "flatten_far_s", 60.0) * 1000),
            flatten_take_ms=int(getattr(self.settings, "flatten_take_s", 120.0) * 1000),
            flatten_hard_ms=int(getattr(self.settings, "flatten_hard_s", 3600.0) * 1000),
            ioc_max_loss_ticks=getattr(self.settings, "ioc_max_loss_ticks", 2),
            stop_loss_bps=getattr(self.settings, "stop_loss_bps", 0.0),
            entry_px=self.entry_px.get(coin, 0.0),
            min_exit_edge_ticks=getattr(self.settings, "min_exit_edge_ticks", 2),
        )

    def _post(self, payload: dict[str, Any]) -> Any:
        if self._in_weight_backoff():
            left = self._weight_backoff_until - time.time()
            raise RequestWeightLimited(f"signed write skipped; request-weight backoff {left:.1f}s left")
        try:
            resp = self.client.post_exchange(payload)
        except RequestWeightLimited as exc:
            self._trip_weight_backoff(exc)
            raise
        except RateLimited as exc:
            if is_weight_limit_error(exc):
                self._trip_weight_backoff(exc)
            raise
        except Exception as exc:  # noqa: BLE001
            if is_weight_limit_error(exc):
                self._trip_weight_backoff(exc)
            raise
        if is_weight_limit_error(resp):
            exc = RequestWeightLimited(str(resp))
            self._trip_weight_backoff(exc)
            raise exc
        if not self._in_weight_backoff():
            self._weight_logged = False
        return resp

    def schedule_deadman(self, ahead_ms: int = DEADMAN_AHEAD_MS) -> None:
        """Account-wide: cancels ALL open orders on the master when it fires."""
        deadline = deadman_deadline_ms(ahead_ms)
        try:
            resp = self._post(self.signer.signed_schedule_cancel(deadline))
            if not action_ok(resp):
                raise LiveGuardError("dead-man schedule rejected")
            now = time.time()
            self.last_deadman = now
            self.deadman_until = deadline / 1000.0
            log.info("dead-man scheduleCancel +%ss account-wide -> %s", ahead_ms // 1000, resp)
        except RequestWeightLimited:
            return
        except RateLimited:
            if self._in_weight_backoff():
                return
            fallback = deadman_deadline_ms(DEADMAN_FALLBACK_MS)
            resp = self._post(self.signer.signed_schedule_cancel(fallback))
            if not action_ok(resp):
                raise LiveGuardError("dead-man fallback rejected")
            now = time.time()
            self.last_deadman = now
            self.deadman_until = fallback / 1000.0
            log.warning("dead-man 429; fallback +%ss account-wide -> %s", DEADMAN_FALLBACK_MS // 1000, resp)

    def maybe_deadman(self) -> None:
        now = time.time()
        if now < self._deadman_retry_at:
            return
        if self._in_weight_backoff():
            return
        if not deadman_needs_refresh(self.deadman_until, now):
            return
        try:
            self.schedule_deadman(DEADMAN_AHEAD_MS)
        except RequestWeightLimited:
            return
        except Exception as exc:  # noqa: BLE001
            if is_weight_limit_error(exc):
                self._trip_weight_backoff(exc)
                return
            if not self._deadman_unavailable_logged:
                log.warning("dead-man unavailable; retry in %.0fs: %s",
                            getattr(self.settings, "deadman_retry_s", 300.0), exc)
                self._deadman_unavailable_logged = True
            self.last_deadman = now
            self._deadman_retry_at = now + getattr(self.settings, "deadman_retry_s", 300.0)
            self.deadman_until = self._deadman_retry_at

    def clear_deadman(self) -> None:
        try:
            resp = self._post(self.signer.signed_schedule_cancel(None))
            if not action_ok(resp):
                raise LiveGuardError("dead-man clear rejected")
            self.deadman_until = 0.0
            log.info("dead-man scheduleCancel cleared (no time) -> %s", resp)
        except RateLimited:
            self.schedule_deadman(DEADMAN_FALLBACK_MS)
        except LiveGuardError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("dead-man clear failed: %s", exc)

    def _cancel_pairs(self, coin: str, oids: list[int], cloids: list[str]) -> bool:
        market = self.markets[coin]
        # A resting order may be represented by both identifiers.  The exchange
        # must receive one cancellation for it: OID is authoritative when known.
        cloids_with_oid = {
            slot.cloid
            for slot in self.rests.get(coin, {}).values()
            if slot.oid in oids and slot.cloid
        }
        cloids = [cloid for cloid in cloids if cloid not in cloids_with_oid]
        ok = True
        if oids:
            try:
                resp = self._post(self.signer.signed_cancel_oids([(market.asset_id, oid) for oid in oids]))
                log.info("cancel oids %s %s -> %s", coin, oids, resp)
                if not action_statuses_ok(resp, len(oids)):
                    ok = False
            except RateLimited:
                log.warning("cancel oids 429 %s", coin)
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("cancel oids %s: %s", coin, exc)
                ok = False
        if cloids:
            try:
                resp = self._post(self.signer.signed_cancel_cloids([(market.asset_id, c) for c in cloids]))
                log.info("cancel cloids %s -> %s", coin, resp)
                if not action_statuses_ok(resp, len(cloids)):
                    ok = False
            except Exception as exc:  # noqa: BLE001
                log.warning("cancel cloids %s: %s", coin, exc)
                ok = False
        return ok

    def cancel_coin_rests(self, *, refetch: bool = True, only_coin: str | None = None) -> dict[str, Any]:
        """Cancel cache then refetch frontendOpenOrders dex:io. 429 does not empty cache."""
        limited = False
        selected = (only_coin,) if only_coin else tuple(self.markets)
        for coin in selected:
            oids = list_cached_oids(self.rests, self.extra_oids, coin)
            cloids = list_cached_cloids(self.rests, coin)
            if oids or cloids:
                try:
                    if self._cancel_pairs(coin, oids, cloids):
                        clear_coin_rests(self.rests, coin)
                        self.extra_oids[coin] = []
                    else:
                        limited = True
                        log.warning("cancel unconfirmed %s; keeping rest cache", coin)
                except RateLimited:
                    limited = True
                    log.warning("cancel cache 429 %s; keeping rest cache", coin)
        if not refetch:
            return {"rateLimited": limited}
        opens = self.client.open_orders(self.signer.account, ALLOWED_DEX, optional=True)
        if opens is None:
            self.open_orders_known = False
            log.warning("frontendOpenOrders null/429; not wiping rest cache")
            try:
                self.schedule_deadman(DEADMAN_FALLBACK_MS)
            except Exception as exc:  # noqa: BLE001
                log.warning("429 fallback scheduleCancel failed: %s", exc)
            return {"rateLimited": True, "cachePreserved": True}
        self.external_orders = [o for o in opens if not is_bot_cloid(o.get("cloid") or o.get("cloidStr"))]
        bot_opens = [o for o in opens if is_bot_cloid(o.get("cloid") or o.get("cloidStr"))]
        self.rests, self.extra_oids = apply_open_orders(self.rests, self.extra_oids, bot_opens, self.markets)
        self.open_orders_known = True
        for coin in selected:
            oids = list_cached_oids(self.rests, self.extra_oids, coin)
            cloids = list_cached_cloids(self.rests, coin)
            if not oids and not cloids:
                continue
            try:
                if self._cancel_pairs(coin, oids, cloids):
                    clear_coin_rests(self.rests, coin)
                    self.extra_oids[coin] = []
                else:
                    limited = True
                    log.warning("cancel unconfirmed after refetch %s; keeping rest cache", coin)
            except RateLimited:
                limited = True
        return {"rateLimited": limited, "fetched": True}

    def _place(self, coin: str, plan: QuotePlan) -> dict[str, Any] | None:
        if not plan.intents:
            return None
        safety_exit = bool(plan.intents) and all(intent.reduce_only for intent in plan.intents)
        if self.leverage_blocked and not safety_exit:
            return {"blocked": "leverageMismatch"}
        if getattr(self.client, "info_cooldown_active", lambda: False)() and not safety_exit:
            return {"blocked": "infoCooldown"}
        if (not self.open_orders_known or self.external_orders) and not safety_exit:
            log.warning("refuse placement %s; open-order state unknown", coin)
            return {"blocked": "openOrdersUnknown"}
        if (not self.position_known and not safety_exit) or (not safety_exit and not self.guardian_ready()):
            log.error("refuse placement %s; safety protection unavailable", coin)
            return {"blocked": "safetyProtectionUnavailable"}
        specs = []
        pending: list[tuple[str, Any]] = []
        for intent in plan.intents:
            if side_occupied(self.rests, coin, intent.side):
                log.warning("refuse duplicate %s %s; oid/cloid already cached", coin, intent.side)
                continue
            cloid = self.signer.next_cloid(intent.coin, intent.side)  # type: ignore[arg-type]
            specs.append(
                (
                    self.markets[coin],
                    intent.is_buy,
                    intent.px,
                    intent.sz,
                    cloid,
                    intent.reduce_only,
                    intent.tif,
                )
            )
            pending.append((intent.side, intent))
        if not specs:
            return None
        self._fee_banner(coin)
        payload = self.signer.signed_orders(specs)
        now = time.time()
        for idx, (side, intent) in enumerate(pending):
            cloid = specs[idx][4]
            if self.intent_wal is not None:
                self.intent_wal.add_pending(cloid, coin, side, {"px": intent.px, "sz": intent.sz})
            self.rests[coin][side] = RestSlot(
                cloid=cloid,
                px=intent.px,
                sz=intent.sz,
                tif=intent.tif,
                reduce_only=intent.reduce_only,
                placed_at=now,
            )
        try:
            resp = self._post(payload)
        except RateLimited:
            log.warning("place 429 %s", coin)
            raise
        if is_alo_reject(resp):
            for idx, (side, _intent) in enumerate(pending):
                if self.intent_wal is not None:
                    self.intent_wal.resolve(specs[idx][4], "rejected")
                self.rests[coin][side] = RestSlot()
            if plan.take:
                log.warning("IOC flatten missed %s %s", coin, resp)
                return {"iocFail": True, "resp": resp}
            log.warning("ALO px rejected, will requote %s %s", coin, resp)
            self.last_plan_key[coin] = ""
            return {"aloReject": True, "resp": resp}
        if not action_ok(resp):
            for idx, (side, _intent) in enumerate(pending):
                if self.intent_wal is not None:
                    self.intent_wal.resolve(specs[idx][4], "rejected")
                self.rests[coin][side] = RestSlot()
            if plan.take:
                log.warning("IOC flatten failed %s %s", coin, resp)
                return {"iocFail": True, "resp": resp}
            if is_alo_reject(resp):
                log.warning("ALO px rejected, will requote %s", coin)
                self.last_plan_key[coin] = ""
                return {"aloReject": True, "resp": resp}
            log.warning("order not ok %s %s", coin, resp)
            return {"resp": resp}
        statuses = resp_statuses(resp)
        accepted = 0
        alo_rejected = False
        for idx, (_side, intent) in enumerate(pending):
            st = statuses[idx] if idx < len(statuses) else None
            err = status_error(st)
            if err:
                if self.intent_wal is not None:
                    self.intent_wal.resolve(specs[idx][4], "rejected")
                self.rests[coin][intent.side] = RestSlot()
                if is_alo_reject(err):
                    log.warning("ALO px rejected, will requote %s %s", coin, err)
                    self.last_plan_key[coin] = ""
                    alo_rejected = True
                    continue
                if plan.take:
                    log.warning("IOC flatten status %s %s", coin, err)
                    return {"iocFail": True, "resp": resp}
                log.warning("order status %s %s", coin, err)
                continue
            if st is None:
                log.warning("missing order status %s %s; retaining pending CLOID", coin, intent.side)
                continue
            if isinstance(st, dict) and "filled" in st:
                if self.intent_wal is not None:
                    self.intent_wal.resolve(specs[idx][4], "filled", status_oid(st))
                self.rests[coin][intent.side] = RestSlot()
                accepted += 1
                continue
            oid = status_oid(st)
            if oid is None:
                log.warning("unrecognized order status %s %s; retaining pending CLOID", coin, st)
                continue
            slot = RestSlot(
                oid=oid,
                cloid=specs[idx][4],
                px=intent.px,
                sz=intent.sz,
                tif=intent.tif,
                reduce_only=intent.reduce_only,
                placed_at=now,
            )
            self.rests[coin][intent.side] = slot
            if self.intent_wal is not None:
                self.intent_wal.resolve(specs[idx][4], "resting", oid)
            accepted += 1
        log.info(
            "%s %s %s",
            "take" if plan.take else "rest",
            coin,
            [(i.side, i.px, i.sz, i.tif, "ro" if i.reduce_only else "") for i in plan.intents],
        )
        self._diag_quotes(coin, accepted, take=plan.take)
        if alo_rejected:
            return {"aloReject": True, "resp": resp}
        return {"resp": resp}

    def requote(self, coin: str, top: BookTop) -> None:
        if self._in_weight_backoff():
            return
        if not self.refresh_positions():
            self.cancel_coin_rests(refetch=False, only_coin=coin)
            return
        if abs(self.pos.get(coin, 0.0)) <= 0 and not self._has_rest(coin):
            self._sample_opening_notional()
        plan = self.plan_for(coin, top)
        key = plan.key()

        have_buy = side_occupied(self.rests, coin, "B")
        have_sell = side_occupied(self.rests, coin, "A")
        want_buy = any(i.is_buy for i in plan.intents)
        want_sell = any(not i.is_buy for i in plan.intents)
        same_coverage = have_buy == want_buy and have_sell == want_sell
        if plan.take:
            now = time.time()
            if now - self.last_take_at.get(coin, 0.0) < IOC_GAP_S:
                self.maybe_deadman()
                return
            log.info("flatten take %s age=%.1fs szi=%s", coin, plan.age_ms / 1000, plan.szi)
            try:
                self.cancel_coin_rests(refetch=True, only_coin=coin)
            except Exception as exc:  # noqa: BLE001
                log.warning("cancel before IOC %s: %s", coin, exc)
            if side_occupied(self.rests, coin, "B") or side_occupied(self.rests, coin, "A"):
                log.warning("still have rests after cancel; skip IOC place %s", coin)
                self.maybe_deadman()
                return
            self.last_take_at[coin] = now
            try:
                self._place(coin, plan)
            except RequestWeightLimited:
                log.warning("IOC skipped %s; request-weight backoff", coin)
            except RateLimited:
                log.warning("IOC 429 %s; continue", coin)
            except Exception as exc:  # noqa: BLE001
                if is_alo_reject(exc):
                    log.warning("ALO/post-only reject on take path %s: %s", coin, exc)
                else:
                    log.warning("IOC flatten retry %s: %s", coin, exc)
            self.last_plan_key[coin] = ""
            self.quote_placed_at[coin] = 0.0
            self.refresh_positions(force=True)
            self.maybe_deadman()
            return
        if not plan.intents:
            log.warning("no maker plan %s; skip", coin)
            self.last_plan_key[coin] = ""
            self.maybe_deadman()
            return
        if same_coverage and key == self.last_plan_key.get(coin):
            extras = list(self.extra_oids.get(coin, []))
            if extras:
                try:
                    if self._cancel_pairs(coin, extras, []):
                        self.extra_oids[coin] = []
                except RequestWeightLimited:
                    return
                except Exception as extra_exc:  # noqa: BLE001
                    log.warning("sweep extras %s: %s", coin, extra_exc)
            self.maybe_deadman()
            return
        elapsed = time.time() - self.last_replace.get(coin, 0.0)
        if not replace_allowed(
            take=False,
            has_rest=have_buy or have_sell,
            elapsed_s=elapsed,
            min_replace_s=self.settings.min_replace_s,
        ):
            if not self._replace_skip_logged.get(coin):
                log.info(
                    "min-replace: keep %s rests (%.1fs < MIN_REPLACE_S=%ss); "
                    "mid/far/flat reprice skipped, take still allowed",
                    coin,
                    elapsed,
                    self.settings.min_replace_s,
                )
                self._replace_skip_logged[coin] = True
            self.maybe_deadman()
            return
        log.info("plan change, cancel then place %s stage=%s tif=%s", coin, plan.stage, plan.tif)
        try:
            self.cancel_coin_rests(refetch=True, only_coin=coin)
        except Exception as exc:  # noqa: BLE001
            if is_alo_reject(exc):
                log.warning("ALO reject during cancel/replace %s: %s", coin, exc)
                self.last_plan_key[coin] = ""
                return
            log.warning("cancel before replace %s: %s", coin, exc)
        try:
            placed = self._place(coin, plan)
        except RequestWeightLimited:
            log.warning("place skipped %s; request-weight backoff", coin)
            self._mark_replaced(coin)
            return
        except RateLimited:
            log.warning("place 429 %s; continue loop", coin)
            self._mark_replaced(coin)
            return
        except Exception as exc:  # noqa: BLE001
            if is_alo_reject(exc):
                log.warning("ALO px rejected, will requote %s: %s", coin, exc)
                self.last_plan_key[coin] = ""
                self._mark_replaced(coin)
                return
            log.warning("place failed %s: %s", coin, exc)
            self._mark_replaced(coin)
            return
        if placed and placed.get("aloReject"):
            self._mark_replaced(coin)
            return
        self.last_plan_key[coin] = key
        self.quote_placed_at[coin] = time.time() if plan.mode == "flat" else 0.0
        self._mark_replaced(coin)
        self.maybe_deadman()

    def step(self, feed: BookFeed | None) -> None:
        self._feed = feed
        if self._fill_reconcile_pending:
            if self.refresh_positions(force=True):
                self.cancel_coin_rests(refetch=False)
                self._fill_reconcile_pending = False
            else:
                return
        try:
            self.refresh_positions()
        except Exception as exc:  # noqa: BLE001
            log.warning("position poll: %s", exc)
            self.position_known = False
        self.enforce_position_leverage()
        try:
            self.poll_user_fills()
        except Exception as exc:  # noqa: BLE001
            log.warning("fill-diag poll: %s", exc)
        self.flush_markouts()
        self.refresh_lease()
        account_safe = (self.guardian_ready() and self.position_known and self.reconcile_orders()
                        and not getattr(self.client, "info_cooldown_active", lambda: False)())
        if not account_safe:
            if not self._uncertain_logged:
                log.warning("account state uncertain; opening quotes suppressed")
                self._uncertain_logged = True
            now = time.time()
            if now - self._last_safety_cancel >= max(self.settings.min_replace_s, 5.0):
                self.cancel_coin_rests(refetch=False)
                self._last_safety_cancel = now
            # Cached inventory may still be flattened through reduce-only plans below.
            if not any(abs(value) > 0 for value in self.pos.values()):
                return
        else:
            self._uncertain_logged = False
        if self.external_orders:
            return
        for coin in self.markets:
            try:
                top = self.book_for(coin, feed)
                if top is None or top.bid is None or top.ask is None:
                    continue
                self.requote(coin, top)
            except Exception as exc:  # noqa: BLE001
                if is_alo_reject(exc):
                    log.warning("ALO px rejected, will requote %s: %s", coin, exc)
                    self.last_plan_key[coin] = ""
                    continue
                log.warning("live step %s: %s", coin, exc)
        self.maybe_deadman()

    def shutdown_cancels(self) -> bool:
        limited = True
        attempts = getattr(self.settings, "shutdown_cancel_attempts", 3)
        for attempt in range(attempts):
            try:
                result = self.cancel_coin_rests(refetch=True)
                limited = bool(result.get("rateLimited"))
                if self.open_orders_known and not any(self._has_rest(c) for c in self.markets):
                    limited = False
                    break
            except Exception as exc:  # noqa: BLE001
                log.warning("shutdown cancel attempt %s: %s", attempt + 1, exc)
            if attempt + 1 < attempts:
                time.sleep(getattr(self.settings, "shutdown_retry_s", 2.0))
        leftover = [
            (self.markets[coin].asset_id, slot.cloid)
            for coin, sides in self.rests.items()
            for slot in sides.values()
            if slot.cloid and is_bot_cloid(slot.cloid)
        ]
        if leftover:
            try:
                resp = self._post(self.signer.signed_cancel_cloids(leftover))
                log.info("shutdown cloid cancel %s", resp)
                if action_statuses_ok(resp, len(leftover)):
                    for sides in self.rests.values():
                        for side, slot in tuple(sides.items()):
                            if slot.cloid and is_bot_cloid(slot.cloid):
                                sides[side] = RestSlot()
                else:
                    limited = True
                    log.warning("shutdown cloid cancel unconfirmed; retaining order cache and protection")
            except Exception as exc:  # noqa: BLE001
                log.warning("shutdown cloid cancel: %s", exc)
                limited = True
        if limited:
            try:
                self.schedule_deadman(DEADMAN_FALLBACK_MS)
            except Exception as exc:  # noqa: BLE001
                log.warning("shutdown dead-man fallback: %s", exc)
        # Bounded authoritative position reads followed by reduce-only IOC.
        flattened = False
        for _ in range(3):
            if not self.refresh_positions(force=True):
                time.sleep(0.2)
                continue
            active = [coin for coin, size in self.pos.items() if abs(size) > 0]
            if not active:
                flattened = True
                break
            for coin in active:
                top = self.book_for(coin, self._feed)
                if top is None:
                    continue
                self.pos_since[coin] = time.time() - getattr(self.settings, "flatten_take_s", 120.0) - 1.0
                plan = self.plan_for(coin, top)
                if plan.take:
                    try:
                        self._place(coin, plan)
                    except Exception as exc:  # noqa: BLE001
                        log.error("shutdown flatten failed %s: %s", coin, exc)
            time.sleep(0.2)
        if flattened and not limited:
            try:
                self.clear_deadman()
            except Exception:
                limited = True
        return flattened and not limited


def _log_agent(client: InfoClient, signer: LiveSigner) -> None:
    if not signer.is_agent:
        return
    extras = client.extra_agents(signer.account, optional=True)
    if extras is None:
        raise LiveGuardError("could not verify agent authorization")
    found = any(
        str(row.get("address") or row.get("agentAddress") or row.get("agent") or "").lower()
        == signer.signer_address.lower()
        for row in extras
        if isinstance(row, dict)
    )
    if found:
        log.info("agent %s is approved on master %s", signer.signer_address, signer.account)
    else:
        raise LiveGuardError(f"agent {signer.signer_address} is not approved on master {signer.account}")


def run_live(settings: Settings, *, seconds: float | None = None) -> int:
    require_live(settings)
    signer = LiveSigner(settings.private_key or "", settings.account, nonce_dir=settings.state_dir)
    account_lock = AccountLock(signer.account, settings.state_dir, ALLOWED_DEX)
    client = InfoClient(
        settings.api_url,
        info_min_interval_s=getattr(settings, "info_min_interval_s", 0.5),
        cooldown_base_s=getattr(settings, "info_cooldown_base_s", 15.0),
        cooldown_max_s=getattr(settings, "info_cooldown_max_s", 120.0),
        proxy_url=getattr(settings, "outbound_proxy_url", None),
    )
    feed: BookFeed | None = None
    quoter: LiveQuoter | None = None
    intent_wal: IntentWAL | None = None
    try:
        _meta, markets, _ctxs = load_io_markets(client, settings.coins)
        dex = perp_dex_info(client.perp_dexs(), ALLOWED_DEX)
        log.info(
            "LIVE isolated MM  dex=%s/%s  master=%s  signer=%s  agent=%s  "
            "notional=$%s  coins=%s  builder=%s fee=0",
            dex.get("name"),
            dex.get("fullName"),
            signer.account,
            signer.signer_address,
            signer.is_agent,
            live_notional(settings),
            ",".join(settings.coins),
            "0xcD254d2A328f7f67C7c6FEf930A4757516F7b601",
        )
        log.info("%s", describe_fee_model(settings))
        log.info("%s", ALO_TIF_NOTE)
        log.info(
            "dead-man is account-wide: scheduleCancel +20s; refresh only when "
            "remaining < %ss (not every few seconds). Failed weight renew: log, no tight-loop. "
            "Official min 5s; 429 fallback +6s. Clear (no time) on clean stop.",
            int(DEADMAN_REMAIN_LT_S),
        )
        log.info(
            "write throttle: MIN_REPLACE_S=%ss per coin while a rest is live "
            "(book ticks do not cancel+replace at the same price). Flatten far=%ss mid, IOC=%ss. "
            "request-weight error backs off signed writes for %ss (log once). "
            "ops: ANTH-only until weight recovers and NY RTH for SNDK "
            "(COINS default still lists both).",
            settings.min_replace_s,
            getattr(settings, "flatten_far_s", 60.0),
            getattr(settings, "flatten_take_s", 120.0),
            weight_backoff_s(settings.min_replace_s),
        )
        log.info(
            "fill-diag observe-only: FILL_DIAG jsonl per fill (+3s markout) and "
            "quote/fill counters split io:ANTH vs io:SNDK. SNDK session=rth|ah "
            "(Mon–Fri 09:30–16:00 America/New_York; no holiday calendar). "
            "AH markout stays in its own bucket."
        )
        _log_agent(client, signer)
        user_state = client.clearinghouse_state(signer.account, ALLOWED_DEX, optional=True)
        parsed = positions_from_state(user_state, tuple(markets), markets)
        if parsed is None:
            raise LiveGuardError("startup position reconciliation failed")
        assert user_state is not None
        log.info("isolated user state withdrawable=%s", user_state.get("withdrawable"))
        opens = client.open_orders(signer.account, ALLOWED_DEX, optional=True)
        if opens is None:
            raise LiveGuardError("startup open-order reconciliation failed")
        intent_wal = IntentWAL(settings.intent_db_path)
        intent_wal.prune_terminal(time.time() - 30 * 86400)
        unresolved = intent_wal.reconcile_open_orders(opens)
        if unresolved:
            fills = client.user_fills(signer.account, optional=True)
            if fills is None:
                raise LiveGuardError("pending WAL reconciliation could not read user fills")
            unresolved = intent_wal.reconcile_history(
                unresolved,
                fills,
                lambda cloid: client.order_status(signer.account, cloid, optional=True),
            )
            if unresolved:
                raise LiveGuardError(f"pending WAL intents remain unresolved: {len(unresolved)}")
            user_state = client.clearinghouse_state(signer.account, ALLOWED_DEX, optional=True)
            parsed = positions_from_state(user_state, tuple(markets), markets)
            opens = client.open_orders(signer.account, ALLOWED_DEX, optional=True)
            if parsed is None or opens is None:
                raise LiveGuardError("post-WAL recovery state refresh failed")
        quoter = LiveQuoter(settings, markets, client, signer, intent_wal=intent_wal)
        quoter.pos = parsed
        external = [o for o in opens if not is_bot_cloid(o.get("cloid") or o.get("cloidStr"))]
        if external:
            raise LiveGuardError(f"external open orders block startup: {len(external)}")
        quoter.rests, quoter.extra_oids = apply_open_orders(quoter.rests, quoter.extra_oids, opens, markets)
        quoter.position_known = True
        quoter.bootstrap_isolated()
        quoter.refresh_lease()
        if not quoter.guardian_ready():
            raise LiveGuardError("external cancellation guardian is not ready and fresh")
        try:
            quoter.schedule_deadman(DEADMAN_AHEAD_MS)
        except Exception as exc:  # scheduleCancel volume gate is expected on this account
            log.warning("exchange dead-man unavailable; external guardian remains mandatory: %s", exc)
        startup_cancel = quoter.cancel_coin_rests(refetch=True)
        if startup_cancel.get("rateLimited") or any(quoter._has_rest(c) for c in markets):
            raise LiveGuardError("startup open orders could not be reconciled and cancelled")
        final_opens = client.open_orders(signer.account, ALLOWED_DEX, optional=True)
        if final_opens is None or final_opens:
            raise LiveGuardError("startup cancellation proof failed")

        stop = {"flag": False}

        def _handle(_signum: int, _frame: object) -> None:
            stop["flag"] = True

        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)

        quoter.poll_user_fills(force=True)
        feed = BookFeed(
            settings.ws_url,
            settings.coins,
            quoter.on_book,
            user=signer.account,
            on_user_fills=quoter.on_user_fills,
            proxy_url=getattr(settings, "outbound_proxy_url", None),
        )
        quoter._feed = feed
        feed.start()
        if not feed.wait_ready():
            log.error("websocket did not become ready")
            feed.stop()
            return 1
        deadline = time.time() + seconds if seconds else None
        while not stop["flag"]:
            if deadline and time.time() >= deadline:
                break
            quoter.step(feed)
            time.sleep(LOOP_S)
        return 0
    finally:
        shutdown_error: Exception | None = None
        if feed is not None:
            feed.stop()
        if quoter is not None:
            try:
                quoter.flush_markouts()
            except Exception as exc:  # noqa: BLE001
                log.warning("fill-diag shutdown flush: %s", exc)
            try:
                if not quoter.shutdown_cancels():
                    raise LiveGuardError("shutdown could not prove orders cancelled and positions flat")
            except Exception as exc:  # noqa: BLE001
                log.error("shutdown: %s", exc)
                shutdown_error = exc
        if intent_wal is not None:
            intent_wal.close()
        client.close()
        account_lock.close()
        if shutdown_error is not None:
            raise shutdown_error
