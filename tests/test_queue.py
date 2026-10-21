"""Database invariants; run in the API container with workers scaled to zero.

docker compose stop worker
docker compose exec -T api python -m unittest tests.test_queue -v
docker compose up -d --scale worker=3 --wait
"""

import os
import unittest
import uuid
from unittest.mock import patch


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "Run database tests inside the API container with workers stopped")
class QueueInvariants(unittest.TestCase):
    def setUp(self):
        from app import db
        from app import leader
        self.db = db
        self.leader = leader
        self.event_id, self.booking_id = uuid.uuid4(), uuid.uuid4()
        self.worker_a, self.worker_b = f"test-{uuid.uuid4().hex[:8]}", f"test-{uuid.uuid4().hex[:8]}"
        self.owner_id = f"queue-test:{uuid.uuid4()}"
        with db.connect() as conn:
            conn.execute("""INSERT INTO events(id,slug,title,date_label,venue,category,description,
                total_seats,available_seats,is_demo) VALUES (%s,%s,'Invariant test','TEST','TEST','TEST','TEST',5,5,true)""",
                         (self.event_id, str(self.event_id)))
            conn.execute("""INSERT INTO bookings(id,event_id,idempotency_key,customer_name,quantity)
                VALUES (%s,%s,%s,'Invariant test',2)""", (self.booking_id, self.event_id, uuid.uuid4()))
        db.register_worker(self.worker_a)
        db.register_worker(self.worker_b)

    def tearDown(self):
        self.leader.release(self.worker_b, self.owner_id)
        with self.db.connect() as conn:
            conn.execute("DELETE FROM booking_activity WHERE booking_id=%s", (self.booking_id,))
            conn.execute("DELETE FROM bookings WHERE id=%s", (self.booking_id,))
            conn.execute("DELETE FROM events WHERE id=%s", (self.event_id,))
            conn.execute("DELETE FROM workers WHERE id IN (%s,%s)", (self.worker_a, self.worker_b))

    def expire(self):
        with self.db.connect() as conn:
            conn.execute("UPDATE bookings SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
                         (self.booking_id,))

    def recover_expired(self):
        result = self.leader.step(self.worker_b, self.owner_id)
        self.assertTrue(result["active"], result)
        self.assertEqual(result["owner_id"], self.owner_id)
        self.assertEqual(result["recovered_count"], 1)

    def state(self):
        with self.db.connect() as conn:
            return conn.execute("""SELECT b.*,e.available_seats FROM bookings b JOIN events e ON e.id=b.event_id
                WHERE b.id=%s""", (self.booking_id,)).fetchone()

    def claim(self, worker):
        result = self.db.claim_booking(worker)
        self.assertIsNotNone(result, "Drain the queue and stop workers before these database tests")
        self.assertEqual(result["id"], self.booking_id, "Database tests require an otherwise idle queue")
        return result

    def test_expired_and_superseded_claims_cannot_debit_inventory(self):
        old = self.claim(self.worker_a)
        self.expire()
        self.assertIsNone(self.db.complete_booking(self.worker_a, old))
        self.assertEqual(self.state()["available_seats"], 5)
        self.recover_expired()
        replacement = self.claim(self.worker_b)
        self.assertEqual(replacement["attempts"], 2)
        self.assertNotEqual(old["claim_token"], replacement["claim_token"])
        self.assertIsNone(self.db.complete_booking(self.worker_a, old))
        self.assertEqual(self.db.complete_booking(self.worker_b, replacement), "confirmed")
        self.assertIsNone(self.db.complete_booking(self.worker_b, replacement))
        self.assertEqual(self.state()["available_seats"], 3)
        with self.db.connect() as conn:
            count = conn.execute("SELECT count(*) AS n FROM booking_activity WHERE booking_id=%s AND kind='confirmed'",
                                 (self.booking_id,)).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_stale_heartbeat_cannot_extend_a_replacement_lease(self):
        old = self.claim(self.worker_a)
        self.expire()
        self.recover_expired()
        replacement = self.claim(self.worker_b)
        before = self.state()["lease_until"]
        self.db.heartbeat(self.worker_a, old)
        after = self.state()
        self.assertEqual(after["claim_token"], replacement["claim_token"])
        self.assertEqual(after["lease_until"], before)
        self.assertEqual(self.db.complete_booking(self.worker_b, replacement), "confirmed")

    def test_commit_failure_rolls_back_inventory_and_confirmation_together(self):
        claim = self.claim(self.worker_a)
        with patch.object(self.db, "activity", side_effect=RuntimeError("injected write failure")):
            with self.assertRaisesRegex(RuntimeError, "injected write failure"):
                self.db.complete_booking(self.worker_a, claim)
        state = self.state()
        self.assertEqual(state["available_seats"], 5)
        self.assertEqual(state["status"], "processing")
        self.assertEqual(self.db.complete_booking(self.worker_a, claim), "confirmed")
        self.assertEqual(self.state()["available_seats"], 3)

    def test_late_heartbeat_does_not_restore_a_completed_assignment(self):
        claim = self.claim(self.worker_a)
        self.assertEqual(self.db.complete_booking(self.worker_a, claim), "confirmed")
        self.db.heartbeat(self.worker_a, claim)
        with self.db.connect() as conn:
            worker = conn.execute("SELECT current_booking_id FROM workers WHERE id=%s", (self.worker_a,)).fetchone()
        self.assertIsNone(worker["current_booking_id"])


if __name__ == "__main__":
    unittest.main()
