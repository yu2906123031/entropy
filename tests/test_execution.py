import unittest

from entropy_mm.execution import ActionResult, ExecutionMode, LIVE_CONFIRMATION, execute_plan
from entropy_mm.reconcile import Cancel, Place, ReconcilePlan


class FakeVenue:
    def __init__(self):
        self.calls = []
        self.cancel_ok = True
        self.remaining = set()
        self.place_ok = True
        self.recover_ok = False

    def cancel_orders(self, ids):
        self.calls.append(("cancel", ids))
        return tuple(ActionResult(str(oid), self.cancel_ok, "test") for oid in ids)

    def open_order_ids(self):
        self.calls.append(("verify",))
        return self.remaining

    def place_orders(self, places):
        self.calls.append(("place", places))
        return tuple(ActionResult(str(p.level), self.place_ok, "test") for p in places)

    def recover_place_results(self, places, results):
        self.calls.append(("recover", places))
        if not self.recover_ok:
            return results
        return tuple(ActionResult(str(p.level), True, "recovered", 7000 + i, "accepted_recovered") for i, p in enumerate(places))


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.venue = FakeVenue()
        self.plan = ReconcilePlan((Cancel(11, "stale"),), (Place(0, "buy", 99.0, 0.1),), ())

    def live(self, **kwargs):
        return execute_plan(self.plan, self.venue, mode=ExecutionMode.LIVE, live_enabled=True, confirmation=LIVE_CONFIRMATION, **kwargs)

    def test_dry_run_makes_zero_venue_calls(self):
        result = execute_plan(self.plan, self.venue)
        self.assertEqual(result.status, "planned_only")
        self.assertEqual(self.venue.calls, [])

    def test_live_requires_both_independent_gates(self):
        for enabled, token in [(False, ""), (True, "wrong"), (False, LIVE_CONFIRMATION)]:
            result = execute_plan(self.plan, self.venue, mode=ExecutionMode.LIVE, live_enabled=enabled, confirmation=token)
            self.assertEqual(result.status, "live_locked")
        self.assertEqual(self.venue.calls, [])

    def test_cancel_is_verified_before_place(self):
        result = self.live()
        self.assertEqual(result.status, "executed")
        self.assertEqual([call[0] for call in self.venue.calls], ["cancel", "verify", "place"])

    def test_rejected_cancel_response_is_recovered_when_order_is_gone(self):
        self.venue.cancel_ok = False
        result = self.live()
        self.assertEqual(result.status, "executed")
        self.assertEqual([call[0] for call in self.venue.calls], ["cancel", "verify", "place"])

    def test_cancel_still_open_blocks_place(self):
        self.venue.remaining = {11, 99}
        result = self.live()
        self.assertEqual(result.status, "cancel_unconfirmed")
        self.assertEqual(result.remaining_cancel_ids, (11,))
        self.assertEqual([call[0] for call in self.venue.calls], ["cancel", "verify"])

    def test_rejected_cancel_and_still_open_is_rejected(self):
        self.venue.cancel_ok = False
        self.venue.remaining = {11}
        self.assertEqual(self.live().status, "cancel_rejected")

    def test_risk_gate_allows_cancels_and_blocks_opening(self):
        result = self.live(allow_opening=False)
        self.assertEqual(result.status, "cancelled_only")
        self.assertEqual([call[0] for call in self.venue.calls], ["cancel", "verify"])

    def test_partial_place_result_requires_resync(self):
        self.venue.place_ok = False
        self.assertEqual(self.live().status, "place_incomplete")
        self.assertIn("recover", [call[0] for call in self.venue.calls])

    def test_missing_place_response_can_be_recovered(self):
        self.venue.place_ok = False
        self.venue.recover_ok = True
        result = self.live()
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.placement_state, "full")

    def test_non_post_only_is_rejected_before_place(self):
        self.plan = ReconcilePlan((), (Place(0, "buy", 99, 0.1, post_only=False),), ())
        result = self.live()
        self.assertEqual(result.status, "unsafe_non_post_only")
        self.assertEqual(self.venue.calls, [])


if __name__ == "__main__":
    unittest.main()
