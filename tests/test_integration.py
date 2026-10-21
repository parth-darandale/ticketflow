"""Live API tests. Every case creates its own hidden demo event.

Ordinary discovery skips these tests when the API is unavailable. The verification
script sets TICKETFLOW_REQUIRE_API=1, making an unavailable API a failure.
"""

import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.error import URLError

from tests.helpers import ApiClient, assert_inventory, submit_batch


class TicketFlowIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = ApiClient()
        try:
            cls.client.request("GET", "/api/dashboard")
        except (URLError, TimeoutError, ConnectionError) as error:
            if os.environ.get("TICKETFLOW_REQUIRE_API") == "1":
                raise AssertionError(f"TicketFlow API is required at {cls.client.base_url}: {error}") from error
            raise unittest.SkipTest(f"Start TicketFlow at {cls.client.base_url} to run live integration tests: {error}") from error

    def test_concurrent_demand_cannot_oversell(self):
        event = self.client.create_event(9, "oversell")
        submitted = submit_batch(self.client, event["id"], 18)
        self.assertEqual(len({booking["id"] for booking in submitted}), 18)
        completed = self.client.wait_for_bookings([booking["id"] for booking in submitted])
        confirmed = [booking for booking in completed.values() if booking["status"] == "confirmed"]
        rejected = [booking for booking in completed.values() if booking["status"] == "rejected"]
        self.assertEqual(len(confirmed), 9)
        self.assertEqual(len(rejected), 9)
        self.assertTrue(all(booking["rejection_reason"] for booking in rejected))
        assert_inventory(self.client.event(event["id"]), completed.values())

    def test_concurrent_idempotent_retries_create_one_booking_and_debit_once(self):
        event = self.client.create_event(6, "idempotency")
        key = str(uuid.uuid4())

        def book(_):
            return self.client.book(event["id"], quantity=2, customer="Same guest", key=key)

        with ThreadPoolExecutor(max_workers=12) as pool:
            outcomes = list(pool.map(book, range(12)))
        identifiers = {booking["id"] for _, booking in outcomes}
        self.assertEqual(len(identifiers), 1)
        self.assertEqual(sum(status == 202 for status, _ in outcomes), 1)
        self.assertEqual(sum(booking["replayed"] is True for _, booking in outcomes), 11)
        completed = self.client.wait_for_bookings(identifiers)
        self.assertEqual(next(iter(completed.values()))["status"], "confirmed")
        status, replay = self.client.book(event["id"], quantity=2, customer="Same guest", key=key, expected=(200,))
        self.assertEqual(replay["id"], next(iter(identifiers)))
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "confirmed")
        assert_inventory(self.client.event(event["id"]), completed.values())

    def test_reusing_key_for_different_payload_returns_conflict(self):
        event = self.client.create_event(4, "key-conflict")
        key = str(uuid.uuid4())
        _, original = self.client.book(event["id"], customer="Original guest", key=key, expected=(202,))
        self.client.book(event["id"], quantity=2, customer="Original guest", key=key, expected=(409,))
        self.client.book(event["id"], customer="Different guest", key=key, expected=(409,))
        completed = self.client.wait_for_bookings([original["id"]])
        assert_inventory(self.client.event(event["id"]), completed.values())

    def test_quantity_is_atomic_and_remaining_inventory_is_usable(self):
        event = self.client.create_event(5, "quantity")
        submitted = submit_batch(self.client, event["id"], 2, quantity=3)
        completed = self.client.wait_for_bookings([booking["id"] for booking in submitted])
        self.assertEqual(sorted(booking["status"] for booking in completed.values()), ["confirmed", "rejected"])
        self.assertEqual(self.client.event(event["id"])["available_seats"], 2)
        _, final = self.client.book(event["id"], quantity=2, customer="Uses remainder", expected=(202,))
        completed.update(self.client.wait_for_bookings([final["id"]]))
        inventory = self.client.event(event["id"])
        self.assertEqual(inventory["available_seats"], 0)
        self.assertEqual(inventory["confirmed_tickets"], 5)
        assert_inventory(inventory, completed.values())

    def test_validation_does_not_create_a_booking(self):
        event = self.client.create_event(2, "validation")
        self.client.request("POST", "/api/demo/events", {"title": "   ", "total_seats": 2}, expected=(422,))
        for quantity in (0, 7):
            with self.subTest(quantity=quantity):
                self.client.book(event["id"], quantity=quantity, expected=(422,))
        self.client.request("POST", "/api/bookings", {"event_id": event["id"], "customer_name": "Guest", "quantity": 1},
                            expected=(400, 422))
        self.client.book(event["id"], customer="", expected=(422,))
        assert_inventory(self.client.event(event["id"]), [])

    def test_demo_event_is_hidden_from_showcase_and_included_when_requested(self):
        event = self.client.create_event(2, "isolation")
        dashboard = self.client.request("GET", "/api/dashboard")[1]
        self.assertNotIn(event["id"], {item["id"] for item in dashboard["events"]})
        dashboard = self.client.request("GET", "/api/dashboard?include_demo=true")[1]
        self.assertIn(event["id"], {item["id"] for item in dashboard["events"]})


if __name__ == "__main__":
    unittest.main()
