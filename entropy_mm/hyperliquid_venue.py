"""Hyperliquid implementation of the Entropy execution Venue protocol."""
from __future__ import annotations

import hashlib
from typing import Any

from hyperliquid.utils.types import Cloid

from .execution import ActionResult
from .reconcile import Place


def _statuses(response: Any) -> list[Any]:
    if not isinstance(response, dict) or response.get("status") != "ok":
        return []
    payload = response.get("response", {})
    return payload.get("data", {}).get("statuses", []) if isinstance(payload, dict) else []


def _category(status: Any) -> str:
    text = str(status).lower()
    if "post only" in text or "immediately matched" in text:
        return "post_only_reject"
    if "margin" in text:
        return "margin_reject"
    if "rate" in text or "429" in text:
        return "rate_limit"
    if "size" in text or "minimum" in text or "min notional" in text:
        return "size_reject"
    if "success" in text or "resting" in text:
        return "accepted"
    if "missing_status" in text:
        return "unknown_state"
    return "venue_reject"


def _cloid_hex(coin: str, place: Place) -> str:
    material = f"entropy|{coin}|{place.quote_epoch}|{place.side}|{place.level}|{place.price}|{place.size}|{place.reduce_only}"
    return "0x" + hashlib.sha256(material.encode()).hexdigest()[:32]


def _cloid(coin: str, place: Place) -> Cloid:
    return Cloid.from_str(_cloid_hex(coin, place))


class HyperliquidVenue:
    """Thin SDK adapter; signing credentials stay inside the injected Exchange."""

    def __init__(self, exchange: Any, info: Any, *, address: str, coin: str):
        self.exchange = exchange
        self.info = info
        self.address = address
        self.coin = coin

    def cancel_orders(self, order_ids: tuple[int, ...]) -> tuple[ActionResult, ...]:
        if not order_ids:
            return ()
        response = self.exchange.bulk_cancel([{"coin": self.coin, "oid": oid} for oid in order_ids])
        statuses = _statuses(response)
        results: list[ActionResult] = []
        for index, oid in enumerate(order_ids):
            status = statuses[index] if index < len(statuses) else {"error": "missing_status"}
            accepted = status == "success" or isinstance(status, dict) and "success" in status
            results.append(ActionResult(str(oid), accepted, str(status), oid, _category(status)))
        return tuple(results)

    def place_orders(self, places: tuple[Place, ...]) -> tuple[ActionResult, ...]:
        requests = []
        for place in places:
            if not place.post_only:
                raise ValueError("Hyperliquid opening quotes require post-only")
            requests.append(
                {
                    "coin": self.coin,
                    "is_buy": place.side == "buy",
                    "sz": place.size,
                    "limit_px": place.price,
                    "order_type": {"limit": {"tif": "Alo"}},
                    "reduce_only": place.reduce_only,
                    "cloid": _cloid(self.coin, place),
                }
            )
        if not requests:
            return ()
        response = self.exchange.bulk_orders(requests)
        statuses = _statuses(response)
        results: list[ActionResult] = []
        for index, place in enumerate(places):
            status = statuses[index] if index < len(statuses) else {"error": "missing_status"}
            reference = f"{place.side}:{place.level}:{place.quote_epoch}"
            resting = status.get("resting", {}) if isinstance(status, dict) else {}
            oid = int(resting["oid"]) if isinstance(resting, dict) and "oid" in resting else None
            accepted = oid is not None
            results.append(ActionResult(reference, accepted, str(status), oid, _category(status)))
        return tuple(results)

    def recover_place_results(self, places: tuple[Place, ...], results: tuple[ActionResult, ...]) -> tuple[ActionResult, ...]:
        """Resolve missing placement responses by matching deterministic CLOIDs."""
        rows = [row for row in self.info.open_orders(self.address) if row.get("coin") == self.coin]
        by_cloid = {str(row.get("cloid", "")).lower(): row for row in rows if row.get("cloid")}
        recovered: list[ActionResult] = []
        for index, place in enumerate(places):
            existing = results[index] if index < len(results) else ActionResult(f"{place.side}:{place.level}:{place.quote_epoch}", False, "missing_status", None, "unknown_state")
            if existing.accepted:
                recovered.append(existing)
                continue
            row = by_cloid.get(_cloid_hex(self.coin, place).lower())
            if row and row.get("oid") is not None:
                recovered.append(ActionResult(existing.reference, True, "recovered_from_open_orders", int(row["oid"]), "accepted_recovered"))
            else:
                recovered.append(existing)
        return tuple(recovered)

    def open_order_ids(self) -> set[int]:
        return {int(order["oid"]) for order in self.info.open_orders(self.address) if order.get("coin") == self.coin}
