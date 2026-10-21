"""Pure Python checks for Lamport ordering, validation and concurrent access."""

import socket
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from app.clock import CLOCK, PROCESS_ID, LamportClock


class LamportClockTests(unittest.TestCase):
    def test_initialization_and_read_only_value(self):
        self.assertEqual(LamportClock().value, 0)
        clock = LamportClock(initial=7)
        self.assertEqual(clock.value, 7)
        with self.assertRaises(AttributeError):
            clock.value = 99
        self.assertEqual(clock.value, 7)

    def test_tick_records_sequential_local_events(self):
        clock = LamportClock()
        self.assertEqual([clock.tick() for _ in range(3)], [1, 2, 3])
        self.assertEqual(clock.value, 3)

    def test_receive_advances_past_remote_and_local_values(self):
        clock = LamportClock(initial=4)
        self.assertEqual(clock.receive(12), 13)
        self.assertEqual(clock.receive(2), 14)
        self.assertEqual(clock.receive(14), 15)
        self.assertEqual(clock.receive(0), 16)

    def test_invalid_initial_timestamps(self):
        for value in (True, False, 1.0, "1", None, [], {}, object()):
            with self.subTest(value=value), self.assertRaises(TypeError):
                LamportClock(value)
        for value in (-1, -100):
            with self.subTest(value=value), self.assertRaises(ValueError):
                LamportClock(value)

    def test_invalid_receive_does_not_mutate_clock(self):
        clock = LamportClock(initial=5)
        for value in (True, False, 1.0, "1", None, [], {}, object()):
            with self.subTest(value=value), self.assertRaises(TypeError):
                clock.receive(value)
            self.assertEqual(clock.value, 5)
        for value in (-1, -100):
            with self.subTest(value=value), self.assertRaises(ValueError):
                clock.receive(value)
            self.assertEqual(clock.value, 5)
        self.assertEqual(clock.tick(), 6)

    def test_concurrent_ticks_are_unique_and_contiguous(self):
        clock = LamportClock(initial=100)
        with ThreadPoolExecutor(max_workers=12) as pool:
            timestamps = list(pool.map(lambda _: clock.tick(), range(2000)))
        self.assertEqual(sorted(timestamps), list(range(101, 2101)))
        self.assertEqual(clock.value, 2100)

    def test_threaded_receives_are_unique_and_observe_remote_events(self):
        clock = LamportClock()
        remote_values = [0, 10, 1000, 3, 999, 50] * 100

        def receive(remote):
            return remote, clock.receive(remote)

        with ThreadPoolExecutor(max_workers=12) as pool:
            received = list(pool.map(receive, remote_values))
        timestamps = [timestamp for _, timestamp in received]
        self.assertEqual(len(set(timestamps)), len(timestamps))
        self.assertTrue(all(timestamp > remote for remote, timestamp in received))
        self.assertEqual(clock.value, max(timestamps))
        self.assertGreater(clock.value, max(remote_values))

    def test_independent_instances_and_message_causality(self):
        sender, receiver = LamportClock(), LamportClock()
        sent_at = sender.tick()
        self.assertEqual(receiver.value, 0)
        received_at = receiver.receive(sent_at)
        reply_at = receiver.tick()
        reply_received_at = sender.receive(reply_at)
        self.assertLess(sent_at, received_at)
        self.assertLess(received_at, reply_at)
        self.assertLess(reply_at, reply_received_at)
        self.assertEqual(receiver.value, reply_at)

    def test_process_globals_identify_one_shared_incarnation(self):
        import app.clock as module

        self.assertIs(module.CLOCK, CLOCK)
        self.assertIsInstance(CLOCK, LamportClock)
        hostname, incarnation = PROCESS_ID.rsplit(":", 1)
        self.assertEqual(hostname, socket.gethostname())
        self.assertEqual(uuid.UUID(hex=incarnation).version, 4)


if __name__ == "__main__":
    unittest.main()
