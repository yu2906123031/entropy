import unittest

from entropy_mm.hyperliquid_venue import HyperliquidVenue
from entropy_mm.reconcile import Place


class FakeExchange:
    def __init__(self):
        self.cancel_response = {"status": "ok", "response": {"data": {"statuses": ["success"]}}}
        self.order_response = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 22}}]}}}
        self.cancel_requests = None
        self.order_requests = None

    def bulk_cancel(self, requests):
        self.cancel_requests = requests
        return self.cancel_response

    def bulk_orders(self, requests):
        self.order_requests = requests
        return self.order_response


class FakeInfo:
    def open_orders(self, address):
        return [{"coin": "HYPE", "oid": 22}, {"coin": "BTC", "oid": 23}]


class HyperliquidVenueTests(unittest.TestCase):
    def setUp(self):
        self.exchange = FakeExchange()
        self.venue = HyperliquidVenue(self.exchange, FakeInfo(), address="0xabc", coin="HYPE")

    def test_cancel_maps_coin_and_order_id(self):
        result = self.venue.cancel_orders((11,))
        self.assertTrue(result[0].accepted)
        self.assertEqual(self.exchange.cancel_requests, [{"coin": "HYPE", "oid": 11}])

    def test_same_plan_retry_keeps_cloid_idempotent(self):
        place = Place(0, "buy", 99.0, 0.1, quote_epoch=123)
        self.venue.place_orders((place,))
        first = self.exchange.order_requests[0]["cloid"].to_raw()
        self.venue.place_orders((place,))
        second = self.exchange.order_requests[0]["cloid"].to_raw()
        self.assertEqual(first, second)

    def test_new_quote_epoch_changes_cloid(self):
        self.venue.place_orders((Place(0, "buy", 99.0, 0.1, quote_epoch=123),))
        first = self.exchange.order_requests[0]["cloid"].to_raw()
        self.venue.place_orders((Place(0, "buy", 99.0, 0.1, quote_epoch=124),))
        second = self.exchange.order_requests[0]["cloid"].to_raw()
        self.assertNotEqual(first, second)

    def test_place_uses_alo(self):
        result = self.venue.place_orders((Place(0, "buy", 99.0, 0.1, quote_epoch=1),))
        request = self.exchange.order_requests[0]
        self.assertTrue(result[0].accepted)
        self.assertEqual(result[0].category, "accepted")
        self.assertEqual(request["order_type"], {"limit": {"tif": "Alo"}})
        self.assertFalse(request["reduce_only"])

    def test_missing_status_fails_closed(self):
        self.exchange.order_response = {"status": "ok", "response": {"data": {"statuses": []}}}
        result = self.venue.place_orders((Place(0, "sell", 101, 0.1),))[0]
        self.assertFalse(result.accepted)
        self.assertEqual(result.category, "unknown_state")

    def test_open_orders_are_filtered_by_coin(self):
        self.assertEqual(self.venue.open_order_ids(), {22})


if __name__ == "__main__":
    unittest.main()
