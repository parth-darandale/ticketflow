"""Live HTTP checks for causal tracing without changing showcase inventory."""

import unittest
import uuid

from tests.helpers import ApiClient, assert_inventory, submit_batch


class BookingTraceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = ApiClient()
        cls.client.request("GET", "/api/health")

    def test_concurrent_bookings_keep_separate_causal_traces(self):
        event = self.client.create_event(6, "logical-traces")
        bookings = submit_batch(self.client, event["id"], 6)
        completed = self.client.wait_for_bookings([b["id"] for b in bookings])
        assert_inventory(self.client.event(event["id"]), completed.values())
        activity_ids = set()
        for booking in completed.values():
            with self.subTest(booking=booking["id"]):
                trace = self.client.request("GET", f"/api/bookings/{booking['id']}/trace")[1]
                self.assertEqual(trace["booking_id"], booking["id"])
                self.assertTrue(trace["instrumented"])
                events = trace["events"]
                self.assertEqual([e["kind"] for e in events], ["queued", "claimed", "confirmed", "observed"])
                queued, claim, final, observed = events
                self.assertEqual(claim["received_time"], queued["logical_time"])
                self.assertEqual(observed["received_time"], final["logical_time"])
                stamps = [e["logical_time"] for e in events]
                self.assertTrue(all(a < b for a, b in zip(stamps, stamps[1:])), stamps)
                self.assertNotEqual(queued["process_id"], claim["process_id"])
                self.assertEqual(claim["process_id"], final["process_id"])
                self.assertEqual(queued["process_id"], observed["process_id"])
                self.assertTrue(observed["ephemeral"])
                self.assertEqual(claim["attempt"], 1)
                ids = {e["id"] for e in events if e["id"] is not None}
                self.assertFalse(ids & activity_ids, "Separate bookings shared persisted trace events")
                activity_ids |= ids

    def test_idempotent_retry_and_observation_do_not_duplicate_trace_events(self):
        event = self.client.create_event(2, "trace-retry")
        key = uuid.uuid4()
        booking = self.client.book(event["id"], key=key)[1]
        self.client.wait_for_bookings([booking["id"]])
        replay = self.client.book(event["id"], key=key, expected=(200,))[1]
        self.assertEqual(booking["id"], replay["id"])
        first = self.client.request("GET", replay["trace_url"])[1]
        second = self.client.request("GET", replay["trace_url"])[1]
        self.assertEqual(first["events"][:-1], second["events"][:-1])
        self.assertEqual(sum(e["kind"] == "queued" for e in second["events"]), 1)
        self.assertGreater(second["events"][-1]["logical_time"], first["events"][-1]["logical_time"])
        assert_inventory(self.client.event(event["id"]), [self.client.booking(booking["id"])])

    def test_unknown_booking_has_no_trace(self):
        self.client.request("GET", f"/api/bookings/{uuid.uuid4()}/trace", expected=(404,))

    def test_coordination_reports_one_live_lease_holder(self):
        snapshot = self.client.request("GET", "/api/coordination")[1]
        self.assertTrue(snapshot["active"], snapshot)
        self.assertTrue(snapshot["worker_id"])
        self.assertTrue(snapshot["owner_id"])
        self.assertGreaterEqual(snapshot["epoch"], 1)
        self.assertTrue(snapshot["history"])


if __name__ == "__main__":
    unittest.main()
