import random
import unittest

from entropy_mm.execution import ActionResult, ExecutionMode, LIVE_CONFIRMATION, execute_plan
from entropy_mm.reconcile import Cancel, Place, ReconcilePlan


class SequenceVenue:
    def __init__(self, *, cancel_ok=True, remaining=(), place_pattern=(True,), recover_pattern=()):
        self.cancel_ok = cancel_ok
        self.remaining = set(remaining)
        self.place_pattern = tuple(place_pattern)
        self.recover_pattern = tuple(recover_pattern)
        self.calls = []

    def cancel_orders(self, ids):
        self.calls.append("cancel")
        return tuple(ActionResult(str(oid), self.cancel_ok, "cancel") for oid in ids)

    def open_order_ids(self):
        self.calls.append("verify")
        return set(self.remaining)

    def place_orders(self, places):
        self.calls.append("place")
        out = []
        for i, place in enumerate(places):
            accepted = self.place_pattern[i] if i < len(self.place_pattern) else False
            out.append(ActionResult(str(place.level), accepted, "place", 8000 + i if accepted else None))
        return tuple(out)

    def recover_place_results(self, places, results):
        self.calls.append("recover")
        out = list(results)
        for i, place in enumerate(places):
            recovered = self.recover_pattern[i] if i < len(self.recover_pattern) else False
            if recovered and (i >= len(out) or not out[i].accepted):
                while len(out) <= i:
                    out.append(ActionResult(str(places[len(out)].level), False, "missing"))
                out[i] = ActionResult(str(place.level), True, "recovered", 9000 + i, "accepted_recovered")
        return tuple(out)


class ExecutionSequenceTests(unittest.TestCase):
    def plan(self):
        return ReconcilePlan(
            (Cancel(11, "stale"),),
            (
                Place(0, "buy", 99.0, 0.1),
                Place(0, "sell", 101.0, 0.1),
            ),
            (),
        )

    def run_live(self, venue):
        return execute_plan(self.plan(), venue, mode=ExecutionMode.LIVE, live_enabled=True, confirmation=LIVE_CONFIRMATION)

    def test_partial_two_sided_placement_stays_incomplete_when_only_one_recovers(self):
        venue = SequenceVenue(place_pattern=(True, False), recover_pattern=(False, False))
        result = self.run_live(venue)
        self.assertEqual(result.status, "place_incomplete")
        self.assertEqual(result.placement_state, "partial")

    def test_two_sided_missing_response_can_fully_recover(self):
        venue = SequenceVenue(place_pattern=(False, False), recover_pattern=(True, True))
        result = self.run_live(venue)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.placement_state, "full")

    def test_cancel_uncertainty_never_allows_place_if_order_remains(self):
        venue = SequenceVenue(cancel_ok=False, remaining=(11,), place_pattern=(True, True))
        result = self.run_live(venue)
        self.assertIn(result.status, {"cancel_rejected", "cancel_unconfirmed"})
        self.assertNotIn("place", venue.calls)

    def test_randomized_recovery_never_reports_executed_without_two_accepts(self):
        rng = random.Random(12345)
        for _ in range(500):
            place_pattern = (rng.choice([True, False]), rng.choice([True, False]))
            recover_pattern = (rng.choice([True, False]), rng.choice([True, False]))
            venue = SequenceVenue(place_pattern=place_pattern, recover_pattern=recover_pattern)
            result = self.run_live(venue)
            if result.status == "executed":
                self.assertEqual(len(result.place_results), 2)
                self.assertTrue(all(item.accepted for item in result.place_results))
                self.assertEqual(result.placement_state, "full")


if __name__ == "__main__":
    unittest.main()
