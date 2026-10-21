"""Election/recovery invariants in disposable PostgreSQL schemas.

Run inside the API container: python -m unittest tests.test_election -v
Unlike the global-queue fixtures, these tests never pause or alter live workers.
Every connection is restricted to a fresh UUID schema, removed during cleanup.
"""

import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "Run inside the API container with PostgreSQL available")
class ElectionInvariants(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        from app import db, leader

        self.db, self.leader = db, leader
        self.schema = "test_election_" + uuid.uuid4().hex
        self.worker_a, self.worker_b = "worker-a", "worker-b"
        self.owner_a, self.owner_b = "process-a-" + uuid.uuid4().hex, "process-b-" + uuid.uuid4().hex
        with psycopg.connect(db.DATABASE_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))

        def cleanup_schema():
            with psycopg.connect(db.DATABASE_URL, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

        self.addCleanup(cleanup_schema)

        def isolated_connect():
            return psycopg.connect(db.DATABASE_URL, row_factory=dict_row, connect_timeout=5,
                                   options=f"-c search_path={self.schema},pg_catalog")

        self.connections = isolated_connect
        self.connect_patch = patch.object(db, "connect", side_effect=isolated_connect)
        self.connect_patch.start()
        self.addCleanup(self.connect_patch.stop)
        db.initialize()
        with db.connect() as conn:
            leader.initialize(conn)
        db.register_worker(self.worker_a)
        db.register_worker(self.worker_b)

    def current(self):
        with self.db.connect() as conn:
            return self.leader.snapshot(conn)

    def expire_leader(self):
        with self.db.connect() as conn:
            conn.execute("UPDATE cluster_leader SET lease_until=clock_timestamp()-interval '1 second'")

    def booking(self):
        event_id, booking_id = uuid.uuid4(), uuid.uuid4()
        with self.db.connect() as conn:
            conn.execute("""INSERT INTO events(id,slug,title,date_label,venue,category,description,
                total_seats,available_seats,is_demo) VALUES (%s,%s,'Election fixture','TEST','TEST','TEST','TEST',5,5,true)""",
                         (event_id, str(event_id)))
            conn.execute("""INSERT INTO bookings(id,event_id,idempotency_key,customer_name,quantity)
                VALUES (%s,%s,%s,'Election fixture',2)""", (booking_id, event_id, uuid.uuid4()))
        claim = self.db.claim_booking(self.worker_a)
        self.assertEqual(claim["id"], booking_id)
        return event_id, booking_id, claim

    def expire_booking(self, booking_id):
        with self.db.connect() as conn:
            conn.execute("UPDATE bookings SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (booking_id,))

    def booking_state(self, booking_id):
        with self.db.connect() as conn:
            return conn.execute("""SELECT b.*,e.available_seats FROM bookings b JOIN events e ON e.id=b.event_id
                WHERE b.id=%s""", (booking_id,)).fetchone()

    def test_concurrent_contenders_elect_one_owner(self):
        barrier = threading.Barrier(6)

        def contend(number):
            barrier.wait(timeout=10)
            return self.leader.step(f"worker-{number}", f"run-{number}")

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(contend, range(6)))
        self.assertEqual(sum(result["is_leader"] for result in results), 1)
        state = self.current()
        self.assertTrue(state["active"])
        self.assertEqual(state["epoch"], 1)
        self.assertEqual(len(state["history"]), 1)

    def test_renewal_keeps_epoch_and_followers_do_not_steal(self):
        first = self.leader.step(self.worker_a, self.owner_a)
        renewed = self.leader.step(self.worker_a, self.owner_a)
        follower = self.leader.step(self.worker_b, self.owner_b)
        self.assertEqual(first["epoch"], renewed["epoch"])
        self.assertGreaterEqual(renewed["lease_until"], first["lease_until"])
        self.assertFalse(follower["is_leader"])
        self.assertEqual(follower["owner_id"], self.owner_a)
        self.assertEqual(len(follower["history"]), 1)

    def test_takeover_changes_owner_and_increases_epoch(self):
        first = self.leader.step(self.worker_a, self.owner_a)
        self.expire_leader()
        replacement = self.leader.step(self.worker_b, self.owner_b)
        self.assertTrue(replacement["is_leader"])
        self.assertEqual(replacement["epoch"], first["epoch"] + 1)
        self.assertEqual(replacement["owner_id"], self.owner_b)

    def test_same_process_reacquisition_still_increases_epoch(self):
        first = self.leader.step(self.worker_a, self.owner_a)
        self.expire_leader()
        second = self.leader.step(self.worker_a, self.owner_a)
        self.assertEqual(second["epoch"], first["epoch"] + 1)
        self.assertEqual(len(second["history"]), 2)

    def test_expired_epoch_cannot_be_renewed_or_used_to_recover(self):
        first = self.leader.step(self.worker_a, self.owner_a)
        self.expire_leader()
        with self.db.connect() as conn:
            self.assertIsNone(self.leader.acquire_or_renew(conn, self.worker_a, self.owner_a,
                                                         expected_epoch=first["epoch"]))
            self.assertEqual(self.leader.recover_expired(conn, self.worker_a, self.owner_a, first["epoch"]), 0)
        self.assertFalse(self.leader.release(self.worker_a, self.owner_a, first["epoch"]))

    def test_stale_owner_and_stale_epoch_cannot_change_new_leader(self):
        first = self.leader.step(self.worker_a, self.owner_a)
        self.expire_leader()
        replacement = self.leader.step(self.worker_b, self.owner_b)
        with self.db.connect() as conn:
            self.assertIsNone(self.leader.acquire_or_renew(conn, self.worker_a, self.owner_a,
                                                         expected_epoch=first["epoch"]))
            self.assertEqual(self.leader.recover_expired(conn, self.worker_a, self.owner_a, first["epoch"]), 0)
            self.assertEqual(self.leader.recover_expired(conn, self.worker_b, self.owner_b, first["epoch"]), 0)
        self.assertFalse(self.leader.release(self.worker_a, self.owner_a, first["epoch"]))
        self.assertFalse(self.leader.release(self.worker_b, self.owner_b, first["epoch"]))
        self.assertEqual(self.current()["epoch"], replacement["epoch"])
        self.assertEqual(self.current()["owner_id"], self.owner_b)

    def test_recovery_preserves_inventory_and_fences_old_claim(self):
        _, booking_id, old_claim = self.booking()
        before = self.booking_state(booking_id)
        self.expire_booking(booking_id)
        elected = self.leader.step(self.worker_b, self.owner_b)
        pending = self.booking_state(booking_id)
        self.assertEqual(elected["recovered_count"], 1)
        self.assertEqual(elected["recovered_total"], 1)
        self.assertEqual(elected["history"][0]["recovered_count"], 1)
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["attempts"], 1)
        self.assertEqual(pending["available_seats"], 5)
        self.assertIsNone(pending["claim_token"])
        self.assertIsNone(pending["worker_id"])
        self.assertIsNone(pending["lease_until"])
        self.assertGreater(pending["last_logical_time"], before["last_logical_time"] or 0)
        with self.db.connect() as conn:
            worker = conn.execute("SELECT current_booking_id FROM workers WHERE id=%s", (self.worker_a,)).fetchone()
            self.assertIsNone(worker["current_booking_id"])
            recovered = conn.execute("SELECT * FROM booking_activity WHERE booking_id=%s AND kind='recovered'", (booking_id,)).fetchone()
            self.assertEqual(recovered["received_time"], before["last_logical_time"] or 0)
        self.assertIsNone(self.db.complete_booking(self.worker_a, old_claim))
        replacement = self.db.claim_booking(self.worker_b)
        self.assertEqual(replacement["id"], booking_id)
        self.assertEqual(replacement["attempts"], 2)
        self.assertEqual(self.db.complete_booking(self.worker_b, replacement), "confirmed")
        self.assertIsNone(self.db.complete_booking(self.worker_b, replacement))
        self.assertEqual(self.booking_state(booking_id)["available_seats"], 3)
        self.assertEqual(self.leader.step(self.worker_b, self.owner_b)["recovered_count"], 0)

    def test_recovery_does_not_touch_live_claim(self):
        _, booking_id, claim = self.booking()
        self.assertEqual(self.leader.step(self.worker_b, self.owner_b)["recovered_count"], 0)
        self.assertEqual(self.booking_state(booking_id)["claim_token"], claim["claim_token"])
        self.assertEqual(self.db.complete_booking(self.worker_a, claim), "confirmed")

    def test_old_heartbeat_cannot_renew_recovered_or_reassigned_job(self):
        _, booking_id, old_claim = self.booking()
        self.expire_booking(booking_id)
        self.leader.step(self.worker_b, self.owner_b)
        self.db.heartbeat(self.worker_a, old_claim)
        self.assertEqual(self.booking_state(booking_id)["status"], "pending")
        self.assertIsNone(self.booking_state(booking_id)["lease_until"])
        replacement = self.db.claim_booking(self.worker_b)
        before = self.booking_state(booking_id)["lease_until"]
        self.db.heartbeat(self.worker_a, old_claim)
        after = self.booking_state(booking_id)
        self.assertEqual(after["lease_until"], before)
        self.assertEqual(after["claim_token"], replacement["claim_token"])

    def test_stale_leader_cannot_recover_newly_expired_booking(self):
        first = self.leader.step(self.worker_a, self.owner_a)
        self.expire_leader()
        self.leader.step(self.worker_b, self.owner_b)
        _, booking_id, _ = self.booking()
        self.expire_booking(booking_id)
        with self.db.connect() as conn:
            self.assertEqual(self.leader.recover_expired(conn, self.worker_a, self.owner_a, first["epoch"]), 0)
        self.assertEqual(self.booking_state(booking_id)["status"], "processing")
        self.assertEqual(self.leader.step(self.worker_b, self.owner_b)["recovered_count"], 1)

    def test_release_is_guarded_and_next_election_advances_epoch(self):
        first = self.leader.step(self.worker_a, self.owner_a)
        self.assertTrue(self.leader.release(self.worker_a, self.owner_a, first["epoch"]))
        self.assertFalse(self.current()["active"])
        self.assertFalse(self.leader.release(self.worker_a, self.owner_a, first["epoch"]))
        self.assertEqual(self.current()["history"][0]["kind"], "released")
        replacement = self.leader.step(self.worker_b, self.owner_b)
        self.assertEqual(replacement["epoch"], first["epoch"] + 1)

    def test_recovery_failure_rolls_back_queue_pointer_and_counters(self):
        _, booking_id, _ = self.booking()
        self.expire_booking(booking_id)
        with patch.object(self.db, "activity", side_effect=RuntimeError("injected recovery failure")):
            with self.assertRaisesRegex(RuntimeError, "injected recovery failure"):
                self.leader.step(self.worker_b, self.owner_b)
        state = self.booking_state(booking_id)
        self.assertEqual(state["status"], "processing")
        self.assertEqual(state["available_seats"], 5)
        self.assertIsNotNone(state["claim_token"])
        with self.db.connect() as conn:
            worker = conn.execute("SELECT current_booking_id FROM workers WHERE id=%s", (self.worker_a,)).fetchone()
            self.assertEqual(worker["current_booking_id"], booking_id)
        self.assertEqual(self.current()["recovered_total"], 0)
        self.assertEqual(self.current()["epoch"], 0)
        self.assertEqual(self.current()["history"], [])

    def test_legacy_trace_keeps_unknown_history_without_fake_timestamps(self):
        from app import api
        from app.clock import CLOCK

        event_id, booking_id = uuid.uuid4(), uuid.uuid4()
        with self.db.connect() as conn:
            conn.execute("""INSERT INTO events(id,slug,title,date_label,venue,category,description,
                total_seats,available_seats,is_demo) VALUES (%s,%s,'Legacy fixture','TEST','TEST','TEST','TEST',5,5,true)""",
                         (event_id, str(event_id)))
            conn.execute("""INSERT INTO bookings(id,event_id,idempotency_key,customer_name,quantity)
                VALUES (%s,%s,%s,'Legacy fixture',2)""", (booking_id, event_id, uuid.uuid4()))
            # Model a row written before the additive clock migration.
            conn.execute("""INSERT INTO booking_activity(booking_id,kind,message)
                VALUES (%s,'queued','Historical queue event without clock instrumentation')""", (booking_id,))
        before = CLOCK.value
        historical = api.booking_trace(booking_id)
        self.assertFalse(historical["instrumented"])
        self.assertEqual(len(historical["events"]), 1)
        self.assertIsNone(historical["events"][0]["logical_time"])
        self.assertIsNone(historical["events"][0]["process_id"])
        self.assertFalse(any(event["kind"] == "observed" for event in historical["events"]))
        self.assertEqual(CLOCK.value, before)

        claim = self.db.claim_booking(self.worker_a)
        self.assertEqual(claim["id"], booking_id)
        instrumented = api.booking_trace(booking_id)
        self.assertTrue(instrumented["instrumented"])
        legacy, received, observed = instrumented["events"]
        self.assertIsNone(legacy["logical_time"])
        self.assertIsNone(legacy["process_id"])
        self.assertEqual(received["kind"], "claimed")
        self.assertIsNone(received["received_time"])
        self.assertGreater(received["logical_time"], 0)
        self.assertTrue(observed["ephemeral"])
        self.assertEqual(observed["received_time"], received["logical_time"])
        self.assertGreater(observed["logical_time"], received["logical_time"])
        api.booking_trace(booking_id)
        with self.db.connect() as conn:
            count = conn.execute("SELECT count(*) AS n FROM booking_activity WHERE booking_id=%s", (booking_id,)).fetchone()["n"]
        self.assertEqual(count, 2, "Trace reads must not append observed events to the database")

    def test_completion_failure_rolls_back_timestamp_inventory_state_and_trace(self):
        from app.clock import CLOCK

        _, booking_id, claim = self.booking()
        before = self.booking_state(booking_id)
        with self.db.connect() as conn:
            event_count = conn.execute("SELECT count(*) AS n FROM booking_activity WHERE booking_id=%s", (booking_id,)).fetchone()["n"]
        clock_before = CLOCK.value
        with patch.object(self.db, "activity", side_effect=RuntimeError("injected completion failure")):
            with self.assertRaisesRegex(RuntimeError, "injected completion failure"):
                self.db.complete_booking(self.worker_a, claim)
        failed_tick = CLOCK.value
        self.assertGreater(failed_tick, clock_before)
        rolled_back = self.booking_state(booking_id)
        for field in ("last_logical_time", "available_seats", "status", "claim_token", "worker_id"):
            self.assertEqual(rolled_back[field], before[field], field)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM booking_activity WHERE booking_id=%s", (booking_id,)).fetchone()["n"], event_count)
        self.assertEqual(self.db.complete_booking(self.worker_a, claim), "confirmed")
        final = self.booking_state(booking_id)
        self.assertEqual(final["available_seats"], 3)
        self.assertGreater(final["last_logical_time"], failed_tick)
        with self.db.connect() as conn:
            events = conn.execute("SELECT * FROM booking_activity WHERE booking_id=%s ORDER BY id", (booking_id,)).fetchall()
        self.assertEqual(len(events), event_count + 1)
        self.assertEqual(events[-1]["kind"], "confirmed")
        self.assertEqual(events[-1]["logical_time"], final["last_logical_time"])

    def test_delayed_worker_clock_report_cannot_lower_current_incarnation(self):
        from app.clock import CLOCK, PROCESS_ID
        high = CLOCK.value + 1000
        with self.db.connect() as conn:
            conn.execute("UPDATE workers SET process_id=%s,logical_time=%s WHERE id=%s",
                         (PROCESS_ID, high, self.worker_a))
        self.db.heartbeat(self.worker_a)
        event_id, booking_id, claim = self.booking()
        self.assertEqual(self.db.complete_booking(self.worker_a, claim), "confirmed")
        with self.db.connect() as conn:
            worker = conn.execute("SELECT * FROM workers WHERE id=%s", (self.worker_a,)).fetchone()
        self.assertGreaterEqual(worker["logical_time"], high)
        # A distinct process incarnation may legitimately start a new clock.
        from app.clock import LamportClock
        with patch.object(self.db, "PROCESS_ID", "replacement-incarnation"), patch.object(self.db, "CLOCK", LamportClock()):
            self.db.register_worker(self.worker_a)
        with self.db.connect() as conn:
            worker = conn.execute("SELECT * FROM workers WHERE id=%s", (self.worker_a,)).fetchone()
        self.assertEqual(worker["logical_time"], 0)
        self.assertEqual(worker["process_id"], "replacement-incarnation")


if __name__ == "__main__":
    unittest.main()
